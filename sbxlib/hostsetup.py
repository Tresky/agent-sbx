"""`sbx setup`: the one-time setup of a new Proxmox host and this Mac, in order.

The wizard asks for the host, reads the host (host/discover.sh), proposes the
values of host/local.conf, then runs each step of docs/setup.md. Each step
has a check, and a step whose check passes is skipped, so a second run
continues where the first one stopped.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import socket
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULTS_ENV, REPO_ROOT, Config, ConfigError, load, local_conf_path, parse_env_file, state_dir
from .templates import load_all
from .run import Runner

PVE_TOKEN_SERVICE = "sbx-pve-token"
PVE_TOKEN_COMMAND = ["security", "find-generic-password", "-s", PVE_TOKEN_SERVICE, "-w"]

# Linked clones need a storage that can snapshot a disk. Plain LVM cannot.
_CLONE_TYPES = ("zfspool", "lvmthin", "rbd", "btrfs", "dir", "nfs", "cifs", "cephfs")
_FILE_TYPES = ("dir", "nfs", "cifs", "cephfs", "btrfs")


class SetupError(RuntimeError):
    pass


# --- reading the host and the Mac --------------------------------------------

def mac_networks(netstat: str) -> list[ipaddress.IPv4Network]:
    """The IPv4 destinations in `netstat -rn -f inet`. macOS drops trailing
    zero octets: "10/24" is 10.0.0.0/24, and "169.254" is 169.254.0.0/16."""
    out = []
    for line in netstat.splitlines():
        dest = line.split(None, 1)[0] if line.strip() else ""
        if not dest or not dest[0].isdigit():
            continue
        addr, _, prefix = dest.partition("/")
        octets = addr.split(".")
        if len(octets) > 4 or not all(o.isdigit() for o in octets):
            continue
        bits = int(prefix) if prefix else (8 * len(octets) if len(octets) < 4 else 32)
        try:
            net = ipaddress.ip_network(".".join(octets + ["0"] * (4 - len(octets))) + f"/{bits}", strict=False)
        except ValueError:
            continue
        if net.is_loopback or net.is_multicast or net.is_link_local or net.prefixlen == 0:
            continue
        out.append(net)
    return out


def host_networks(disc: dict) -> list[ipaddress.IPv4Network]:
    out = []
    for cidr in [a["cidr"] for a in disc.get("addresses", [])] + [r["dst"] for r in disc.get("routes", [])]:
        if not cidr or cidr == "default":
            continue
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if not (net.is_loopback or net.is_link_local):
            out.append(net)
    return out


@dataclass
class Choice:
    key: str
    value: str
    why: str


def _pick_storage(storage: list[dict], content: str, types: tuple[str, ...]) -> dict | None:
    fits = [s for s in storage if s.get("active") and content in s.get("content", "").split(",")
            and s.get("type") in types]
    return min(fits, key=lambda s: (types.index(s["type"]), s["name"] != "local", s["name"]), default=None)


def _free_ids(used: set[int]) -> int | None:
    """The first base B where B (the first template), B+1 (the gateway) and
    B+100..B+199 (the sandboxes) are all free. The templates take the free ids
    of B..B+99 as they are built."""
    for base in range(9000, 999_000, 1000):
        if not ({base, base + 1} | set(range(base + 100, base + 200))) & used:
            return base
    return None


def _free_subnets(taken: list[ipaddress.IPv4Network]) -> tuple[str, str] | None:
    candidates = [(f"10.{i}.0", f"10.{i + 1}.0") for i in range(77, 250, 2)]
    candidates += [(f"172.{i}.0", f"172.{i + 1}.0") for i in range(28, 31, 2)]
    for pair in candidates:
        nets = [ipaddress.ip_network(f"{p}.0/24") for p in pair]
        if not any(n.overlaps(t) for n in nets for t in taken):
            return pair
    return None


def propose(disc: dict, mac_nets: list[ipaddress.IPv4Network], current: dict[str, str]) -> list[Choice]:
    """Values for host/local.conf. A key that `current` (host/local.conf as it
    is) already sets keeps its value: a second run must not move a working
    setup to new bridges or new ids."""
    out: list[Choice] = []

    def add(key: str, value: str, why: str):
        if key in current:
            out.append(Choice(key, current[key], "already in host/local.conf"))
        else:
            out.append(Choice(key, value, why))

    bridges = {b["name"]: b for b in disc.get("bridges", [])}
    default_dev = next((r["dev"] for r in disc.get("routes", []) if r.get("dst") == "default"), "")
    if default_dev in bridges:
        add("SBX_LAN_BRIDGE", default_dev, "the bridge of the host's default route")
    elif bridges:
        lan = next((b for b in bridges.values() if b.get("gateway")), next(iter(bridges.values())))
        add("SBX_LAN_BRIDGE", lan["name"], "the first bridge with an address")
    else:
        raise SetupError("the host has no Linux bridge; make one for the LAN in the Proxmox web UI first")

    n = 77
    while f"vmbr{n}" in bridges or f"vmbr{n + 1}" in bridges:
        n += 2
    add("SBX_AGENT_BRIDGE", f"vmbr{n}", "a free bridge name")
    add("SBX_PERSONAL_BRIDGE", f"vmbr{n + 1}", "a free bridge name")

    taken = host_networks(disc) + mac_nets
    pair = _free_subnets(taken)
    if pair is None:
        raise SetupError("no free pair of /24 subnets; set SBX_AGENT_NET and SBX_PERSONAL_NET by hand")
    add("SBX_AGENT_NET", pair[0], "collides with no route on the host or on this Mac")
    add("SBX_PERSONAL_NET", pair[1], "collides with no route on the host or on this Mac")

    used = {g["vmid"] for g in disc.get("guests", [])}
    base = _free_ids(used)
    if base is None:
        raise SetupError("no free block of VM ids; set the SBX_*VMID* keys by hand")
    add("SBX_TEMPLATE_VMID_MIN", str(base), "the templates take free ids from here")
    add("SBX_TEMPLATE_VMID_MAX", str(base + 99), "to here")
    add("SBX_GW_CTID", str(base + 1), "a free id")
    add("SBX_VMID_MIN", str(base + 100), "a free range of 100 ids")
    add("SBX_VMID_MAX", str(base + 199), "a free range of 100 ids")

    storage = disc.get("storage", [])
    for key, content, types, what in (
            ("SBX_VM_STORAGE", "images", _CLONE_TYPES, "VM disks, linked clones"),
            ("SBX_GW_STORAGE", "rootdir", _CLONE_TYPES, "container disks"),
            ("SBX_GW_TEMPLATE_STORAGE", "vztmpl", _FILE_TYPES, "container templates")):
        hit = _pick_storage(storage, content, types)
        if hit is None:
            raise SetupError(f"no active storage holds {what} ({content}); add one in the Proxmox web UI")
        add(key, hit["name"], f"{hit['type']}, holds {what}")
    snip = _pick_storage(storage, "snippets", _FILE_TYPES) or _pick_storage(storage, "iso", _FILE_TYPES) \
        or _pick_storage(storage, "vztmpl", _FILE_TYPES)
    if snip is None:
        raise SetupError("no file storage for the cloud-init snippet; add a directory storage")
    add("SBX_SNIPPET_STORAGE", snip["name"], "a file storage for the cloud-init snippet")
    return out


def check_choices(values: dict[str, str], disc: dict, mac_nets: list[ipaddress.IPv4Network],
                  current: dict[str, str]) -> list[str]:
    """Problems with values that the user typed. A value that host/local.conf
    already had is not checked against the host: that setup made it."""
    problems = []
    domain = values.get("SBX_DOMAIN", "")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        problems.append(f"SBX_DOMAIN {domain!r} is not a DNS name like sbx.internal")
    elif domain.endswith(".local"):
        problems.append("SBX_DOMAIN must not end in .local: macOS sends .local names to mDNS")
    fresh = {k: v for k, v in values.items() if current.get(k) != v}
    taken = host_networks(disc) + mac_nets
    for key in ("SBX_AGENT_NET", "SBX_PERSONAL_NET"):
        if key in fresh:
            try:
                net = ipaddress.ip_network(f"{fresh[key]}.0/24")
            except ValueError:
                problems.append(f"{key} must be three octets, like 10.77.0")
                continue
            if any(net.overlaps(t) for t in taken):
                problems.append(f"{key} {fresh[key]}.0/24 collides with a route on the host or on this Mac")
    if values.get("SBX_AGENT_NET") == values.get("SBX_PERSONAL_NET"):
        problems.append("SBX_AGENT_NET and SBX_PERSONAL_NET must differ")
    used = {g["vmid"] for g in disc.get("guests", [])}
    for key in ("SBX_GW_CTID",):
        if key in fresh and fresh[key].isdigit() and int(fresh[key]) in used:
            problems.append(f"{key} {fresh[key]} is taken by a VM or a container")
    return problems


def render_policy(cfg: Config) -> str:
    text = (REPO_ROOT / "tailscale" / "policy.example.hujson").read_text()
    return (text.replace("10.77.0.0/24", f"{cfg.agent_net}.0/24")
                .replace("10.78.0.0/24", f"{cfg.personal_net}.0/24")
                .replace("tag:sbx-gw", cfg.tailscale_tag))


def set_toml_keys(path: Path, values: dict[str, str | list[str]]) -> None:
    """Set top-level keys of a flat TOML file; keep every other line."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, seen = [], set()
    for line in lines:
        m = re.match(r"\s*#?\s*([a-z_]+)\s*=", line)
        key = m.group(1) if m else None
        if key in values and key not in seen:
            out.append(f"{key} = {json.dumps(values[key])}")
            seen.add(key)
        elif key in values:
            continue
        else:
            out.append(line)
    out += [f"{k} = {json.dumps(v)}" for k, v in values.items() if k not in seen]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)


# --- the wizard ---------------------------------------------------------------

def _ask(question: str, default: str = "") -> str:
    got = input(f"{question}" + (f" [{default}]" if default else "") + ": ").strip()
    return got or default


def _yes(question: str, default: bool = True) -> bool:
    got = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not got else got.startswith("y")


def _pause(text: str) -> None:
    input(f"{text} Press Enter to continue. ")


def token_id(hostname: str | None = None) -> str:
    """This Mac's own token: "cli-" and the short host name. Each Mac has its
    own, so a new token for one Mac does not stop the others."""
    short = (hostname or socket.gethostname()).split(".")[0].lower()
    return "cli-" + (re.sub(r"[^a-z0-9-]+", "-", short).strip("-") or "mac")


class Wizard:
    def __init__(self, args, runner: Runner, api=None):
        from . import cli
        self.cli, self.args, self.runner, self.api = cli, args, runner, api
        self.cfg: Config = load()
        self.target = ""

    # ssh through the shared connection: the root password is asked once.
    def ssh(self, command: str, *, tty: bool = False, capture: bool = True, check: bool = False,
            input: bytes | None = None):
        argv = ["ssh", *self.cli._host_ssh(self.cfg)] + (["-t"] if tty else []) + [self.target, command]
        return self.runner.run(argv, capture=capture, check=check, input=input)

    def step(self, title: str) -> None:
        self.n += 1
        print(f"\n\033[1m== {self.n}. {title}\033[0m", flush=True)

    def run(self) -> int:
        warn = self.cli.warn
        if sys.platform != "darwin":
            warn("sbx keeps its secrets in the macOS keychain; on another system the token steps fail")
        for tool in ("ssh", "scp"):
            if shutil.which(tool) is None:
                raise SetupError(f"{tool} is not installed")
        self.n = 0
        host = self._connect()
        disc = self._discover()
        if self.args.mac_only:
            self._fetch_values(disc)
        else:
            self._host_steps(disc)
        self.step("the API token for this Mac")
        self._token(disc, host)
        self._mac_steps()

        self.step("the checks")
        from . import doctor
        code = doctor.run(load(), self.runner, self.api)
        if code == 0:
            print("\nThe setup is complete. Prove the isolation with: sbx doctor --isolation")
            print("Then make a sandbox: sbx new lab")
        return code

    def _connect(self) -> str:
        self.step("the Proxmox host")
        host = self.args.host or urllib.parse.urlsplit(self.cfg.pve_api).hostname or ""
        host = _ask("Address of the Proxmox host (an IP address or a DNS name)", host)
        if not host:
            raise SetupError("no host address")
        self.cli._prepare_mac(self.cfg, self.runner)
        set_toml_keys(state_dir() / "config.toml", {"pve_api": f"https://{host}:8006"})
        self.cfg = load()
        self.target = self.cfg.pve_ssh_target
        print(f"ssh asks for the root password of {self.target} ONE time; the connection stays open.")
        print("On the first connection, ssh shows the host key. Compare it with the output of\n"
              "  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub\n"
              "in the Shell of the node in the Proxmox web UI, then accept it.")
        if self.ssh("true", capture=False).code != 0:
            raise SetupError(f"cannot open a root shell on {self.target}")
        return host

    def _discover(self) -> dict:
        got = self.ssh("bash -s", input=(REPO_ROOT / "host" / "discover.sh").read_bytes())
        try:
            disc = json.loads(got.stdout)
        except ValueError:
            raise SetupError(f"host/discover.sh printed no JSON: {got.stderr.strip()[:300]}") from None
        if disc.get("error"):
            raise SetupError(disc["error"])
        self.cli.info(f"{disc['node']}: {disc.get('version', '')}")
        return disc

    def _fetch_values(self, disc: dict) -> None:
        """--mac-only: the host holds the values that its setup used, in the
        copy of host/local.conf under /root/sbx. This Mac takes the same."""
        self.step("the values of this setup, from the host")
        if self.ssh("test -d /root/sbx/host").code != 0:
            raise SetupError("the host has no /root/sbx/host, so it is not set up. Run `sbx setup` without --mac-only")
        # No local.conf on the host: that setup used the shared defaults only.
        got = self.ssh("cat /root/sbx/host/local.conf")
        remote = got.stdout if got.code == 0 else ""
        local = local_conf_path()
        here = local.read_text() if local.exists() else ""
        if here and parse_env_file(here) != parse_env_file(remote):
            print("host/local.conf on this Mac differs from the copy on the host:")
            for key in sorted(set(parse_env_file(here)) | set(parse_env_file(remote))):
                a, b = parse_env_file(here).get(key), parse_env_file(remote).get(key)
                if a != b:
                    print(f"  {key}: this Mac {a!r}, the host {b!r}")
            if not _yes("Replace the file on this Mac with the copy from the host?"):
                raise SetupError("stopped; the two copies of host/local.conf must agree")
        if here != remote:
            local.write_text(remote)
            self.cli.info(f"wrote {local} from the host")
        self._fetch_local_templates()
        self.cfg = load()
        if not any(g["vmid"] == self.cfg.gw_ctid for g in disc.get("guests", [])):
            raise SetupError(f"the gateway {self.cfg.gw_ctid} does not exist on the host; "
                             "run `sbx setup` without --mac-only")
        self._tailscale_dns()

    def _host_steps(self, disc: dict) -> None:
        info = self.cli.info
        self.step("the values of this setup (host/local.conf)")
        mac = self.runner.run(["netstat", "-rn", "-f", "inet"], check=False).stdout
        mac_nets = mac_networks(mac)
        local = local_conf_path()
        current = parse_env_file(local.read_text()) if local.exists() else {}
        if any(g["vmid"] == self.cfg.gw_ctid and g.get("name") == self.cfg.gw_hostname
               for g in disc.get("guests", [])):
            # An installation exists: its gateway holds the bridges, the subnets
            # and the ids that the defaults and host/local.conf give now. New
            # values would move the setup away from what the host has.
            info(f"the gateway {self.cfg.gw_ctid} exists; the current values stay")
            current = {**parse_env_file(DEFAULTS_ENV.read_text()), **current}
        choices = [Choice("SBX_DOMAIN", current.get("SBX_DOMAIN", self.cfg.domain),
                          "already in host/local.conf" if "SBX_DOMAIN" in current else "the DNS zone for the sandboxes")]
        choices += propose(disc, mac_nets, current)
        values = self._confirm_values(choices, disc, mac_nets, current)
        if not current.get("SBX_GW_LAN_IP") and not _yes("Does the LAN of the host have a DHCP server?"):
            values["SBX_GW_LAN_IP"] = _ask("Static address for the gateway on the LAN, with its prefix (e.g. 192.168.1.2/24)")
            router = next((r["gateway"] for r in disc.get("routes", []) if r.get("dst") == "default"), "")
            values["SBX_GW_LAN_GW"] = _ask("The router of that subnet", router)
        from . import versions
        versions.write_local_conf(local, values, header="# The values of this setup. `sbx setup` wrote them; "
                                                        "host/defaults.conf explains each key.")
        info(f"wrote {local}")
        self.cfg = load()

        self.step("copy the scripts and the template definitions to the host")
        self.cli._copy_to_host(self.cfg, self.runner)

        c = self.cfg
        self.step(f"the sandbox bridges {c.agent_bridge} and {c.personal_bridge}")
        # The agent bridge must be VLAN-aware: each sandbox and its sidecar
        # share a VLAN. A bridge from before sidecars exists but is not; the
        # script adds the lines.
        if self.ssh(f"ip link show {c.agent_bridge} >/dev/null 2>&1 && ip link show {c.personal_bridge} >/dev/null 2>&1 "
                    f"&& [ \"$(cat /sys/class/net/{c.agent_bridge}/bridge/vlan_filtering 2>/dev/null)\" = 1 ]").code == 0:
            info("the bridges exist, and the agent bridge is VLAN-aware; skipped")
        else:
            print("CAUTION: this step changes /etc/network/interfaces on the host. The script shows the\n"
                  "change and asks before it applies it. It keeps a backup.")
            self._host_script("10-bridges.sh")

        self.step("the Tailscale policy")
        logged_in = self.ssh(f"pct exec {c.gw_ctid} -- tailscale status >/dev/null 2>&1").code == 0
        if logged_in:
            info("the gateway is on the tailnet already; skipped")
        else:
            print("CAUTION: the policy applies to your whole tailnet. Merge these parts into it;\n"
                  "do not paste them over a policy that has other rules.\n")
            print(render_policy(c))
            print("\n1. Open the Tailscale admin console, then Access controls.\n"
                  "2. Merge the parts above into the policy, and save it.\n"
                  "3. Make sure that no rule has \"*\" as a source.")
            _pause("Do this now.")

        self.step(f"the gateway container {c.gw_ctid}")
        if self.ssh(f"pct status {c.gw_ctid} 2>/dev/null | grep -q running").code == 0:
            info("the gateway is running; skipped (to refresh its config: ssh to the host and run host/20-gw-create.sh)")
        else:
            self._host_script("20-gw-create.sh")
        if not logged_in:
            print("The next command prints a login URL. Open it and sign in to your tailnet.")
            self._host_script("20-gw-create.sh --tailscale")
        self._tailscale_dns()

        self._templates(disc)

    def _templates(self, disc: dict) -> None:
        """Adopt a template from before named templates, or build the first
        ones. A setup with named templates already skips the step."""
        self.step("the templates")
        info = self.cli.info
        ours = [g for g in disc.get("guests", []) if g.get("template") and "sbx-template" in g.get("tags", [])]
        named = [g for g in ours if any(tag.startswith("sbx-tpl-") for tag in g.get("tags", []))]
        legacy = [g for g in ours if g not in named]
        conf = state_dir() / "config.toml"
        if legacy:
            g = legacy[-1]
            print(f"VM {g['vmid']} ({g['name']}) is a template from before named templates.")
            if _yes(f"Adopt it as the template 'default'? It stays as it is; `sbx new` uses it by that name."):
                self._host_script(f"30-template-build.sh --adopt {g['vmid']} default")
                if not self.cfg.default_template:
                    set_toml_keys(conf, {"default_template": "default"})
                if "default" not in load_all():
                    print("Write its definition, so that you can rebuild it: sbx template new default --from rails")
        if named:
            info("templates exist: " + ", ".join(sorted({g["name"] for g in named}))
                 + "; skipped (`sbx template list` shows them)")
            self._sidecar_template(named)
            return
        if legacy:
            self._sidecar_template(named)
            return
        defs = load_all()
        print("Each template is a definition in templates/ (shared) or templates/local/ (yours):")
        for name, d in defs.items():
            print(f"  {name:<10} {d.description}  [core{', ' if d.components else ''}{', '.join(d.components)}]")
        while True:
            wanted = _ask("Which templates to build now, separated by spaces", "minimal").split()
            unknown = [n for n in wanted if n not in defs]
            if wanted and not unknown:
                break
            self.cli.warn(f"no definition for {', '.join(unknown) or 'nothing'}; choose from the list")
        print(f"Each build takes 15 to 40 minutes. If the SSH session drops, run: sbx template finish <name>")
        for name in wanted:
            self._host_script(f"30-template-build.sh {name}")
        if not self.cfg.default_template:
            set_toml_keys(conf, {"default_template": wanted[0]})
            info(f"default_template = {wanted[0]} in config.toml; `sbx new --template <name>` picks another")
        self.cfg = load()
        self._sidecar_template([])

    def _sidecar_template(self, named: list[dict]) -> None:
        """Every agent sandbox needs a sidecar, cloned from the sidecar
        template. Build it when the setup has none."""
        c = self.cfg
        if not c.agent_sidecar:
            return
        if any(f"sbx-tpl-{c.sidecar_template}" in g.get("tags", []) for g in named):
            return
        print(f"Each agent sandbox gets a sidecar: a small VM that holds its credentials and its\n"
              f"port policy. It is cloned from the template '{c.sidecar_template}', which takes a few minutes.")
        if _yes(f"Build the sidecar template '{c.sidecar_template}' now?"):
            self._host_script(f"30-template-build.sh {c.sidecar_template}")
        else:
            self.cli.warn(f"no sidecar template: `sbx new` refuses an agent sandbox until you run "
                          f"`sbx template rebuild {c.sidecar_template}`, or set agent_sidecar = false")

    def _fetch_local_templates(self) -> None:
        """--mac-only: the host has the local definitions and components that
        its setup built; this Mac takes the ones that it does not have."""
        import base64
        import io
        import tarfile
        got = self.ssh("cd /root/sbx 2>/dev/null && tar -cf - $(ls -d templates/local "
                       "template/components/local 2>/dev/null) 2>/dev/null | base64")
        data = base64.b64decode(got.stdout) if got.code == 0 and got.stdout.strip() else b""
        if not data:
            return
        taken, differ = [], []
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                if not member.isfile() or member.name.startswith("/") or ".." in Path(member.name).parts:
                    continue
                dest = REPO_ROOT / member.name
                body = tar.extractfile(member).read()
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(body)
                    taken.append(member.name)
                elif dest.read_bytes() != body:
                    differ.append(member.name)
        if taken:
            self.cli.info("from the host: " + ", ".join(taken))
        if differ:
            self.cli.warn("these differ from the host's copy, and this Mac keeps its own: " + ", ".join(differ))

    def _mac_steps(self) -> None:
        self.step("this Mac")
        # Again, now with this setup's domain in the SSH block.
        self.cli._prepare_mac(self.cfg, self.runner)
        if self.cli._has_ssh_include():
            self.cli.info("~/.ssh/config already includes the sbx block")
        elif _yes("Add `Include ~/.config/sbx/ssh_config` to the top of ~/.ssh/config? "
                  "`ssh sbx-<name>` and herdr need it."):
            self.cli._add_ssh_include()
        if shutil.which("mkcert") is None:
            self.cli.warn("mkcert is not installed; sandboxes serve http:// only. Install: brew install mkcert")
        elif not self.cli._mkcert_root(self.runner):
            if _yes("Run `mkcert -install` now? It asks for your Mac password one time."):
                self.runner.run(["mkcert", "-install"], capture=False, check=False)

    def _confirm_values(self, choices: list[Choice], disc, mac_nets, current) -> dict[str, str]:
        while True:
            width = max(len(ch.key) for ch in choices)
            for ch in choices:
                print(f"  {ch.key:<{width}}  {ch.value:<18}  {ch.why}")
            answer = input("Use these values? [Y]es, [e]dit, [q]uit: ").strip().lower()
            if answer.startswith("q"):
                raise KeyboardInterrupt
            if answer.startswith("e"):
                choices = [Choice(ch.key, _ask(f"  {ch.key}", ch.value), "you chose it") for ch in choices]
            values = {ch.key: ch.value for ch in choices}
            problems = check_choices(values, disc, mac_nets, current)
            if not problems:
                return values
            for p in problems:
                self.cli.warn(p)

    def _host_script(self, script: str) -> None:
        done = self.ssh(f"bash /root/sbx/host/{script}", tty=True, capture=False)
        if done.code != 0:
            raise SetupError(f"host/{script} failed; it printed why. Correct it, then run `sbx setup` again")

    def _tailscale_dns(self) -> None:
        c = self.cfg
        name, want = f"{c.gw_hostname}.{c.domain}", f"{c.agent_net}.1"
        while True:
            try:
                got = socket.gethostbyname(name)
            except OSError:
                got = ""
            if got == want:
                self.cli.info(f"{name} resolves to {want} on this Mac")
                return
            print(f"\n{name} does not resolve to {want} on this Mac yet. In the Tailscale admin console:\n"
                  f"1. Machines: make sure that {c.gw_hostname} shows the two subnet routes as approved.\n"
                  f"2. DNS: turn on MagicDNS if it is off.\n"
                  f"3. DNS, Nameservers, Add nameserver, Custom: enter {want}, turn on\n"
                  f"   \"Restrict to domain\", and enter {c.domain}.\n"
                  f"Make sure that Tailscale is connected on this Mac.")
            if input("Press Enter to check again, or type s to skip: ").strip().lower().startswith("s"):
                self.cli.warn("DNS is not ready; `sbx doctor` checks it again later")
                return

    def _token(self, disc: dict, host: str) -> None:
        info = self.cli.info
        conf = state_dir() / "config.toml"
        values: dict = {"pve_token_command": PVE_TOKEN_COMMAND}
        have = self.runner.run(["security", "find-generic-password", "-s", PVE_TOKEN_SERVICE], check=False).code == 0
        if have and not _yes("The keychain has a Proxmox token. Keep it?"):
            have = False
        if have:
            self._host_script("40-api-token.sh --acl-only")
        else:
            got = self.ssh(f"bash /root/sbx/host/40-api-token.sh --rotate --emit --token-id {token_id()}")
            line = next((l for l in got.stdout.splitlines() if l.startswith("SBX_TOKEN=")), "")
            if got.code != 0 or not line:
                raise SetupError(f"host/40-api-token.sh failed: {got.stderr.strip()[-400:]}")
            self.runner.run(["security", "add-generic-password", "-U", "-s", PVE_TOKEN_SERVICE, "-a", "sbx",
                             "-w", line.removeprefix("SBX_TOKEN=")])
            info(f"stored the token in the keychain as {PVE_TOKEN_SERVICE}")

        # The CA survives a certificate renewal, but it verifies the name in
        # pve_api only when that name is in the certificate. Else: the fingerprint.
        sans = {s.split(":", 1)[-1].strip() for s in disc.get("sans", [])}
        if disc.get("ca") and host in sans:
            ca = state_dir() / "pve-root-ca.crt"
            ca.write_text(disc["ca"].rstrip() + "\n")
            values.update(pve_ca_file=str(ca).replace(str(Path.home()), "~"), pve_fingerprint="")
            info(f"saved the host CA as {ca}")
        elif disc.get("fingerprint"):
            values.update(pve_fingerprint=disc["fingerprint"], pve_ca_file="")
            info("the host certificate does not name this address; sbx pins its fingerprint. "
                 "Run `sbx setup` again after Proxmox renews the certificate")
        set_toml_keys(conf, values)
        self.cfg = load()


def cmd_setup(args, cfg: Config, runner: Runner, api=None) -> int:
    try:
        return Wizard(args, runner, api).run()
    except (SetupError, EOFError) as exc:
        raise ConfigError(str(exc) or "no input") from None
