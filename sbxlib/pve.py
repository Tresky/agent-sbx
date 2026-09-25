"""Proxmox operations through the HTTP API, with a SCOPED API token.

Not SSH as root: a root key on the Mac hands the whole hypervisor to every
process that can read it. host/40-api-token.sh creates a token that can manage
only the VMs of one pool, clone only the template, and use only the two
sandbox bridges. The last point matters most: the gateway's rules key on the
bridge, and with this token even a compromised Mac cannot put an agent VM on
the LAN bridge.

VM metadata lives in TAGS, because tags come back in the one cluster-resources
call that `list` already makes:  sbx ; sbx-agent|sbx-personal ; sbx-exp-YYYYMMDD
"""
from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import json
import re
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import projects
from .config import Config, ConfigError
from .run import Runner

_TOKEN_RE = re.compile(r"^[^@\s]+@[^!\s]+![^=\s]+=[0-9a-fA-F-]{36}$")
TASK_TIMEOUT = 900.0


class PveError(RuntimeError):
    pass


class HttpApi:
    """Callable transport: api(method, path, params) -> the `data` of the reply."""

    def __init__(self, cfg: Config, runner: Runner):
        if not cfg.pve_api:
            raise ConfigError("pve_api is not set in config.toml, e.g. \"https://192.168.1.10:8006\"")
        if not cfg.pve_token_command:
            raise ConfigError("pve_token_command is not set in config.toml; run `sbx setup`")
        if not cfg.pve_ca_file and not cfg.pve_fingerprint:
            # Proxmox ships a self-signed certificate. Never fall back to an
            # unverified connection: the token would go to whoever answers.
            raise ConfigError("set pve_ca_file or pve_fingerprint in config.toml; run `sbx setup`")
        self.cfg, self.runner, self._token = cfg, runner, None
        url = urllib.parse.urlsplit(cfg.pve_api)
        if url.scheme != "https" or not url.hostname:
            raise ConfigError(f"pve_api must be an https URL, not {cfg.pve_api!r}")
        self.host, self.port = url.hostname, url.port or 8006

    def _auth(self) -> str:
        if self._token is None:
            token = self.runner.run(self.cfg.pve_token_command).stdout.strip()
            if not _TOKEN_RE.match(token):
                raise ConfigError("pve_token_command must print USER@REALM!TOKENID=SECRET")
            self._token = token
        return f"PVEAPIToken={self._token}"

    def _connect(self) -> http.client.HTTPSConnection:
        if self.cfg.pve_ca_file:
            ctx = ssl.create_default_context(cafile=str(Path(self.cfg.pve_ca_file).expanduser()))
            # Python 3.13+ sets VERIFY_X509_STRICT, which refuses a CA that
            # lacks some RFC 5280 extensions, as a private CA often does. The
            # chain and the host name are still verified without it.
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(self.host, self.port, context=ctx, timeout=30)
        try:
            conn.connect()
        except (OSError, ssl.SSLError) as exc:
            raise PveError(f"cannot connect to {self.cfg.pve_api}: {exc}") from None
        if not self.cfg.pve_ca_file:
            # The pin is checked BEFORE the request, so the token is never sent
            # to a server that shows a different certificate.
            seen = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            want = self.cfg.pve_fingerprint.replace(":", "").lower()
            if seen != want:
                conn.close()
                pretty = ":".join(seen[i:i + 2] for i in range(0, 64, 2)).upper()
                raise PveError(f"{self.cfg.pve_api} shows certificate {pretty}, not pve_fingerprint. "
                               "Proxmox renews its certificate; if that is the cause, update config.toml.")
        return conn

    def __call__(self, method: str, path: str, params: dict | None = None):
        auth = self._auth()
        encoded = urllib.parse.urlencode(params or {})
        url, body = "/api2/json" + path, None
        if method in ("GET", "DELETE"):
            url += f"?{encoded}" if encoded else ""
        else:
            body = encoded
        conn = self._connect()
        try:
            headers = {"Authorization": auth}
            if body is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            conn.request(method, url, body=body, headers=headers)
            resp = conn.getresponse()
            text = resp.read().decode(errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            raise PveError(f"{method} {path}: {exc}") from None
        finally:
            conn.close()
        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError:
            payload = {}
        if resp.status >= 400:
            detail = payload.get("errors") or payload.get("message") or text.strip()[:200]
            hint = " (does the token have this permission? see host/40-api-token.sh)" if resp.status == 403 else ""
            raise PveError(f"{method} {path}: {resp.status} {resp.reason}: {detail}{hint}")
        return payload.get("data")


@dataclass
class Sandbox:
    vmid: int
    hostname: str
    node: str
    status: str
    tags: tuple[str, ...]

    @property
    def profile(self) -> str:
        return "personal" if "sbx-personal" in self.tags else "agent"

    @property
    def project(self) -> str:
        for tag in self.tags:
            if tag.startswith("sbx-proj-"):
                return tag[9:]
        return ""

    @property
    def template(self) -> str:
        # A sandbox from before named templates is a clone of the one old
        # template, which counts as "default".
        for tag in self.tags:
            if tag.startswith("sbx-tpl-"):
                return tag[8:]
        return "default"

    @property
    def expires(self) -> dt.date | None:
        for tag in self.tags:
            if tag.startswith("sbx-exp-"):
                try:
                    return dt.datetime.strptime(tag[8:], "%Y%m%d").date()
                except ValueError:
                    return None
        return None


@dataclass
class TemplateVm:
    """One built version of a template. host/30-template-build.sh tags it
    sbx-template, sbx-tpl-<name> and sbx-h-<fingerprint>. A template from
    before named templates has the first tag only: it counts as "default"."""
    vmid: int
    node: str
    vm_name: str
    name: str
    fingerprint: str


def _tags(resource: dict) -> tuple[str, ...]:
    return tuple(t for t in (resource.get("tags") or "").replace(",", ";").split(";") if t)


class Pve:
    def __init__(self, cfg: Config, api):
        self.cfg, self.api = cfg, api

    # --- plumbing ---
    def _vm(self, node: str, vmid: int, tail: str = "") -> str:
        return f"/nodes/{node}/qemu/{vmid}{tail}"

    def _wait(self, node: str, data):
        """Most changes return a task id and finish later. Block until it ends."""
        if not (isinstance(data, str) and data.startswith("UPID:")):
            return data
        path = f"/nodes/{node}/tasks/{urllib.parse.quote(data, safe='')}/status"
        deadline = time.monotonic() + TASK_TIMEOUT
        while True:
            status = self.api("GET", path) or {}
            if status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise PveError(f"task failed: {status.get('exitstatus')} ({data.split(':')[5]})")
                return None
            if time.monotonic() > deadline:
                raise PveError(f"task did not finish in {int(TASK_TIMEOUT)} s: {data}")
            time.sleep(1)

    # --- queries ---
    def resources(self) -> list[dict]:
        return [r for r in self.api("GET", "/cluster/resources", {"type": "vm"}) or []]

    def templates(self, resources: list[dict] | None = None) -> dict[str, list[TemplateVm]]:
        """Every built template, by name; each name's versions oldest first.
        The newest version is the one that a new sandbox clones."""
        out: dict[str, list[TemplateVm]] = {}
        for r in self.resources() if resources is None else resources:
            tags = _tags(r)
            if r.get("type") != "qemu" or not r.get("template") or "sbx-template" not in tags:
                continue
            name = next((t[8:] for t in tags if t.startswith("sbx-tpl-")), "default")
            fp = next((t[6:] for t in tags if t.startswith("sbx-h-")), "legacy")
            out.setdefault(name, []).append(TemplateVm(int(r["vmid"]), r.get("node", ""), r.get("name", ""), name, fp))
        for versions in out.values():
            versions.sort(key=lambda v: (v.vm_name, v.vmid))
        return dict(sorted(out.items()))

    def template(self, name: str) -> TemplateVm:
        built = self.templates()
        if name not in built:
            known = ", ".join(built) or "none"
            raise PveError(f"template {name!r} is not built (built: {known}). "
                           f"Build it: sbx template rebuild {name}")
        return built[name][-1]

    def sandboxes(self) -> list[Sandbox]:
        out = []
        for r in self.resources():
            tags = _tags(r)
            if r.get("type") != "qemu" or r.get("template") or "sbx" not in tags:
                continue
            out.append(Sandbox(int(r["vmid"]), r.get("name", ""), r.get("node", ""), r.get("status", ""), tags))
        return sorted(out, key=lambda s: s.hostname)

    def find(self, hostname: str) -> Sandbox | None:
        return next((s for s in self.sandboxes() if s.hostname == hostname), None)

    def require(self, hostname: str) -> Sandbox:
        box = self.find(hostname)
        if box is None:
            raise PveError(f"no sandbox named {hostname}")
        return box

    def sidecars(self) -> dict[str, Sandbox]:
        """Every sidecar, by the hostname of the sandbox it serves. A sidecar
        carries sbx-sidecar and sbx-of-<hostname>, and not the sbx tag, so
        sandboxes() never lists it."""
        out = {}
        for r in self.resources():
            tags = _tags(r)
            if r.get("type") != "qemu" or r.get("template") or "sbx-sidecar" not in tags:
                continue
            owner = next((t[7:] for t in tags if t.startswith("sbx-of-")), "")
            if owner:
                out[owner] = Sandbox(int(r["vmid"]), r.get("name", ""), r.get("node", ""), r.get("status", ""), tags)
        return out

    def next_vmid(self, start: int | None = None) -> int:
        # The token sees only its own pool, so an id that another guest holds
        # is invisible in resources(). /cluster/nextid asks the cluster itself.
        for vmid in range(start or self.cfg.vmid_min, self.cfg.vmid_max + 1):
            try:
                self.api("GET", "/cluster/nextid", {"vmid": vmid})
                return vmid
            except PveError as exc:
                if "400" not in str(exc):
                    raise
        raise PveError(f"no free VM id in {self.cfg.vmid_min}-{self.cfg.vmid_max}; run `sbx gc` or `sbx rm`")

    def guest_ipv4(self, box_node: str, vmid: int, prefix: str = "") -> str | None:
        """The first IPv4 address the guest agent reports, or the first one
        that starts with `prefix`: a sidecar has two, and only the one on the
        sidecar network is reachable."""
        try:
            data = self.api("GET", self._vm(box_node, vmid, "/agent/network-get-interfaces")) or {}
        except PveError:
            return None  # the agent is not up yet
        for iface in data.get("result", []):
            if iface.get("name") == "lo":
                continue
            for addr in iface.get("ip-addresses", []):
                if addr.get("ip-address-type") == "ipv4" and addr["ip-address"].startswith(prefix):
                    return addr["ip-address"]
        return None

    # --- lifecycle ---
    def create(self, template: TemplateVm, vmid: int, hostname: str, profile: str, pubkey: str, *,
               cores: int, memory_mb: int, disk_gb: int | None, expires: dt.date | None,
               project: str = "", vlan: int | None = None, ipconfig: str = "ip=dhcp",
               nameserver: str = "", searchdomain: str = "") -> str:
        """Returns the node the sandbox lives on. With `vlan`, the NIC carries
        that tag: the sandbox then shares a segment with its sidecar only."""
        cfg, node = self.cfg, template.node
        # A template clones as a LINKED clone by default: seconds, not minutes.
        # `pool` puts the VM where the token's permissions apply.
        self._wait(node, self.api("POST", self._vm(node, template.vmid, "/clone"),
                                  {"newid": vmid, "name": hostname, "pool": cfg.pve_pool}))
        tags = ["sbx", f"sbx-{profile}", f"sbx-tpl-{template.name}"] + ([f"sbx-exp-{expires:%Y%m%d}"] if expires else [])
        if project:
            tags.append(projects.tag(project))
        params = {
            # The bridge IS the profile: the gateway's rules key on the
            # interface, and a root user in the VM cannot move it. The tag,
            # when there is one, is set on the host side of the tap: root in
            # the VM cannot move that either.
            "net0": f"virtio,bridge={cfg.bridge_for(profile)}" + (f",tag={vlan}" if vlan else ""),
            "cores": cores, "memory": memory_mb,
            "ciuser": cfg.vm_user, "ipconfig0": ipconfig,
            # The API wants this one value URL-encoded INSIDE the form body,
            # so it is encoded twice on the wire.
            "sshkeys": urllib.parse.quote(pubkey.strip(), safe=""),
            "tags": ";".join(tags),
        }
        if nameserver:
            params["nameserver"] = nameserver
        if searchdomain:
            params["searchdomain"] = searchdomain
        self.api("PUT", self._vm(node, vmid, "/config"), params)
        if disk_gb:
            self._wait(node, self.api("PUT", self._vm(node, vmid, "/resize"), {"disk": "scsi0", "size": f"{disk_gb}G"}))
        return node

    def create_sidecar(self, template: TemplateVm, vmid: int, hostname: str, *, vlan: int, pubkey: str) -> str:
        """The sidecar of the sandbox `hostname`: net0 on the sandbox's VLAN
        with the wire address, net1 untagged on the agent bridge with DHCP from
        the gateway. Tagged sbx-sidecar and sbx-of-<hostname>, never sbx, so it
        is not a sandbox to `sbx list`, `sbx rm` or `sbx gc`."""
        cfg, node = self.cfg, template.node
        self._wait(node, self.api("POST", self._vm(node, template.vmid, "/clone"),
                                  {"newid": vmid, "name": f"{hostname}-sc", "pool": cfg.pve_pool}))
        self.api("PUT", self._vm(node, vmid, "/config"), {
            "net0": f"virtio,bridge={cfg.agent_bridge},tag={vlan}",
            "net1": f"virtio,bridge={cfg.agent_bridge}",
            "cores": 1, "memory": 1024,
            "ciuser": cfg.vm_user,
            "ipconfig0": f"ip={cfg.sidecar_addr}/30",
            "ipconfig1": "ip=dhcp",
            "sshkeys": urllib.parse.quote(pubkey.strip(), safe=""),
            "tags": f"sbx-sidecar;sbx-of-{hostname};sbx-tpl-{template.name}",
        })
        return node

    def start(self, node: str, vmid: int):
        self._wait(node, self.api("POST", self._vm(node, vmid, "/status/start")))

    def stop(self, node: str, vmid: int):
        try:
            self._wait(node, self.api("POST", self._vm(node, vmid, "/status/stop")))
        except PveError:
            pass  # already stopped

    def destroy(self, node: str, vmid: int):
        self.stop(node, vmid)
        self._wait(node, self.api("DELETE", self._vm(node, vmid), {"purge": 1, "destroy-unreferenced-disks": 1}))

    def snapshot(self, node: str, vmid: int, label: str):
        self._wait(node, self.api("POST", self._vm(node, vmid, "/snapshot"), {"snapname": label}))

    def rollback(self, node: str, vmid: int, label: str):
        self._wait(node, self.api("POST", self._vm(node, vmid, f"/snapshot/{urllib.parse.quote(label, safe='')}/rollback")))
        # A snapshot without RAM state leaves the VM stopped after a rollback.
        try:
            self.start(node, vmid)
        except PveError:
            pass

    # --- gpu ---
    # A token cannot name a raw PCI address; only root can. It CAN use a
    # Resource Mapping (Datacenter > Resource Mappings), which root defines once.
    def gpu_holder(self) -> tuple[int, str] | None:
        """(vmid, name) of the VISIBLE VM that holds the mapping. A VM outside
        the pool is invisible to the token; Proxmox then refuses the start."""
        needle = f"mapping={self.cfg.gpu_mapping}"
        for r in self.resources():
            if r.get("type") != "qemu":
                continue
            conf = self.api("GET", self._vm(r["node"], int(r["vmid"]), "/config")) or {}
            if any(k.startswith("hostpci") and needle in str(v) for k, v in conf.items()):
                return int(r["vmid"]), r.get("name", "")
        return None

    def gpu_attach(self, node: str, vmid: int):
        # pcie=1 needs the q35 machine type, which the template sets. No memory
        # balloon: a passed-through device pins all guest memory.
        self.api("PUT", self._vm(node, vmid, "/config"),
                 {"hostpci0": f"mapping={self.cfg.gpu_mapping},pcie=1", "balloon": 0})

    def gpu_detach(self, node: str, vmid: int):
        self.api("PUT", self._vm(node, vmid, "/config"), {"delete": "hostpci0"})
