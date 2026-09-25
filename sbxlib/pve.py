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
    def expires(self) -> dt.date | None:
        for tag in self.tags:
            if tag.startswith("sbx-exp-"):
                try:
                    return dt.datetime.strptime(tag[8:], "%Y%m%d").date()
                except ValueError:
                    return None
        return None


class Pve:
    def __init__(self, cfg: Config, api):
        self.cfg, self.api, self._template_node = cfg, api, None

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

    def template_node(self) -> str:
        if self._template_node is None:
            hit = next((r for r in self.resources() if int(r["vmid"]) == self.cfg.template_vmid), None)
            if hit is None:
                raise PveError(f"template {self.cfg.template_vmid} is not visible to the token; "
                               "run host/30-template-build.sh, then host/40-api-token.sh")
            self._template_node = hit["node"]
        return self._template_node

    def sandboxes(self) -> list[Sandbox]:
        out = []
        for r in self.resources():
            tags = tuple(t for t in (r.get("tags") or "").replace(",", ";").split(";") if t)
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

    def next_vmid(self) -> int:
        # The token sees only its own pool, so an id that another guest holds
        # is invisible in resources(). /cluster/nextid asks the cluster itself.
        for vmid in range(self.cfg.vmid_min, self.cfg.vmid_max + 1):
            try:
                self.api("GET", "/cluster/nextid", {"vmid": vmid})
                return vmid
            except PveError as exc:
                if "400" not in str(exc):
                    raise
        raise PveError(f"no free VM id in {self.cfg.vmid_min}-{self.cfg.vmid_max}; run `sbx gc` or `sbx rm`")

    def guest_ipv4(self, box_node: str, vmid: int) -> str | None:
        try:
            data = self.api("GET", self._vm(box_node, vmid, "/agent/network-get-interfaces")) or {}
        except PveError:
            return None  # the agent is not up yet
        for iface in data.get("result", []):
            if iface.get("name") == "lo":
                continue
            for addr in iface.get("ip-addresses", []):
                if addr.get("ip-address-type") == "ipv4":
                    return addr["ip-address"]
        return None

    # --- lifecycle ---
    def create(self, vmid: int, hostname: str, profile: str, pubkey: str, *, cores: int,
               memory_mb: int, disk_gb: int | None, expires: dt.date | None,
               project: str = "") -> str:
        """Returns the node the sandbox lives on."""
        cfg, node = self.cfg, self.template_node()
        # A template clones as a LINKED clone by default: seconds, not minutes.
        # `pool` puts the VM where the token's permissions apply.
        self._wait(node, self.api("POST", self._vm(node, cfg.template_vmid, "/clone"),
                                  {"newid": vmid, "name": hostname, "pool": cfg.pve_pool}))
        tags = ["sbx", f"sbx-{profile}"] + ([f"sbx-exp-{expires:%Y%m%d}"] if expires else [])
        if project:
            tags.append(projects.tag(project))
        self.api("PUT", self._vm(node, vmid, "/config"), {
            # The bridge IS the profile: the gateway's rules key on the
            # interface, and a root user in the VM cannot move it.
            "net0": f"virtio,bridge={cfg.bridge_for(profile)}",
            "cores": cores, "memory": memory_mb,
            "ciuser": cfg.vm_user, "ipconfig0": "ip=dhcp",
            # The API wants this one value URL-encoded INSIDE the form body,
            # so it is encoded twice on the wire.
            "sshkeys": urllib.parse.quote(pubkey.strip(), safe=""),
            "tags": ";".join(tags),
        })
        if disk_gb:
            self._wait(node, self.api("PUT", self._vm(node, vmid, "/resize"), {"disk": "scsi0", "size": f"{disk_gb}G"}))
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
