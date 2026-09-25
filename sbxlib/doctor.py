"""`sbx doctor`: the checks of docs/setup.md, run in one pass.

Each check prints ok, WARN or FAIL, with the next step for a problem. A WARN
is a limit (no https, a relayed path); a FAIL stops the daily commands.
`--isolation` adds the probe pair of docs/setup.md step 8: it makes one sandbox
in each profile, proves what each can reach, and destroys them.
"""
from __future__ import annotations

import ipaddress
import shutil
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .config import Config, local_conf_path, state_dir
from .run import Runner
from .vm import Vm

TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


@dataclass
class Check:
    status: str   # "ok", "WARN", "FAIL"
    name: str
    detail: str = ""


def _tailscale() -> str | None:
    return shutil.which("tailscale") or (TAILSCALE_APP if Path(TAILSCALE_APP).exists() else None)


def mac_checks(cfg: Config, runner: Runner) -> list[Check]:
    from . import cli
    out = []
    local = local_conf_path()
    out.append(Check("ok", "host/local.conf", str(local)) if local.exists() and local.stat().st_size
               else Check("WARN", "host/local.conf", "missing: the shared defaults apply. `sbx setup` writes it"))
    conf = state_dir() / "config.toml"
    missing = [k for k in ("pve_api", "pve_token_command") if not getattr(cfg, k)]
    if not (cfg.pve_ca_file or cfg.pve_fingerprint):
        missing.append("pve_ca_file or pve_fingerprint")
    out.append(Check("FAIL", "config.toml", "not set: " + ", ".join(missing) + ". Run `sbx setup`") if missing
               else Check("ok", "config.toml", str(conf)))
    out.append(Check("ok", "sandbox SSH key", str(cfg.ssh_key_path)) if cfg.ssh_key_path.exists()
               else Check("FAIL", "sandbox SSH key", "missing. Run `sbx setup --mac-only`"))
    out.append(Check("ok", "~/.ssh/config Include") if cli._has_ssh_include()
               else Check("WARN", "~/.ssh/config Include", "missing: `ssh sbx-<name>` and herdr do not work. "
                                                          "Run `sbx setup --mac-only`"))
    if shutil.which("mkcert") is None:
        out.append(Check("WARN", "mkcert", "not installed: sandboxes serve http:// only. `brew install mkcert`"))
    elif not cli._mkcert_root(runner):
        out.append(Check("WARN", "mkcert", "no local CA: sandboxes serve http:// only. Run `mkcert -install`"))
    else:
        out.append(Check("ok", "mkcert"))
    return out


def token_path_allowed(cfg: Config, path: str) -> bool:
    """The paths where host/40-api-token.sh gives the token a right. A VM in
    the sandbox or the template id range inherits its rights from a pool (a
    template from before named templates had a right on its own id)."""
    allowed = {f"/pool/{cfg.pve_pool}", f"/pool/{cfg.template_pool}", f"/storage/{cfg.vm_storage}",
               f"/sdn/zones/localnetwork/{cfg.agent_bridge}", f"/sdn/zones/localnetwork/{cfg.personal_bridge}"}
    if cfg.gpu_mapping:
        allowed.add(f"/mapping/pci/{cfg.gpu_mapping}")
    if path in allowed:
        return True
    vmid = path.removeprefix("/vms/")
    return path.startswith("/vms/") and vmid.isdigit() and (
        cfg.vmid_min <= int(vmid) <= cfg.vmid_max or cfg.template_vmid_min <= int(vmid) <= cfg.template_vmid_max)


def template_checks(cfg: Config, pve) -> list[Check]:
    """One line per built template: current, out of date, or from before
    named templates. A FAIL when none is built, or the default is missing."""
    from . import cli
    built = pve.templates()
    if not built:
        return [Check("FAIL", "templates", "none is built. Build one: sbx template rebuild <name>")]
    out = []
    if cfg.default_template and cfg.default_template not in built:
        out.append(Check("FAIL", "default template", f"'{cfg.default_template}' is not built: "
                                                     f"sbx template rebuild {cfg.default_template}"))
    try:
        defs = cli._definitions()
    except Exception as exc:
        return out + [Check("WARN", "templates", f"a definition does not load: {exc}")]
    for name, versions in built.items():
        newest = versions[-1]
        label = f"template {name}"
        old = f"; {len(versions) - 1} old version(s) kept for sandboxes" if len(versions) > 1 else ""
        if newest.fingerprint == "legacy" and newest.vm_name.startswith("sbx-base"):
            out.append(Check("WARN", label, f"VM {newest.vmid} is from before named templates. "
                                            f"Adopt it: sbx template adopt {newest.vmid} {name}"))
            continue
        state = cli._state(defs.get(name), versions)
        if state == "current":
            out.append(Check("ok", label, f"{newest.vm_name}{old}"))
        elif state == "no definition":
            out.append(Check("WARN", label, f"{newest.vm_name} has no definition on this Mac; "
                                            f"`sbx template new {name}` writes one"))
        else:
            out.append(Check("WARN", label, f"{newest.vm_name} is older than its definition: "
                                            f"sbx template rebuild {name}{old}"))
    return out


def api_checks(cfg: Config, runner: Runner, api=None) -> list[Check]:
    from . import cli
    from .pve import PveError
    try:
        pve = cli._pve(cfg, runner, api)
        version = pve.api("GET", "/version") or {}
    except Exception as exc:  # a config error, a TLS error, a token error: all one FAIL
        return [Check("FAIL", "Proxmox API", f"{exc}")]
    out = [Check("ok", "Proxmox API", f"{cfg.pve_api}, Proxmox {version.get('version', '?')}")]
    try:
        out += template_checks(cfg, pve)
    except PveError as exc:
        return out + [Check("FAIL", "templates", str(exc))]
    try:
        perms = pve.api("GET", "/access/permissions") or {}
    except PveError as exc:
        return out + [Check("WARN", "token scope", f"cannot read: {exc}")]
    wide = [p for p, privs in perms.items() if any(privs.values()) and not token_path_allowed(cfg, p)]
    out.append(Check("FAIL", "token scope", "the token has rights on " + ", ".join(wide)
                     + "; it must have only the rights of host/40-api-token.sh") if wide
               else Check("ok", "token scope", f"pool {cfg.pve_pool}, the templates, the two sandbox bridges"))
    return out


def network_checks(cfg: Config, runner: Runner) -> list[Check]:
    out = []
    gw_ip, name = f"{cfg.agent_net}.1", f"{cfg.gw_hostname}.{cfg.domain}"
    try:
        got = socket.gethostbyname(name)
    except OSError:
        got = ""
    out.append(Check("ok", "DNS", f"{name} -> {gw_ip}") if got == gw_ip
               else Check("FAIL", "DNS", f"{name} -> {got or 'nothing'}, not {gw_ip}. In the Tailscale admin console, "
                                         f"add the nameserver {gw_ip}, restricted to {cfg.domain}"))
    ping = runner.run(["ping", "-c1", "-t3", gw_ip], check=False)
    out.append(Check("ok", "gateway", f"{gw_ip} answers") if ping.code == 0
               else Check("FAIL", "gateway", f"{gw_ip} does not answer. Is Tailscale connected, and are the "
                                             "subnet routes approved?"))
    ts = _tailscale()
    if ts is None:
        out.append(Check("WARN", "Tailscale path", "no tailscale command on this Mac; not checked"))
        return out
    done = runner.run([ts, "ping", "-c", "5", cfg.gw_hostname], check=False, timeout=30)
    text = done.stdout + done.stderr
    if "via DERP" in text and " via " not in text.replace("via DERP", ""):
        out.append(Check("WARN", "Tailscale path", "relayed through DERP: slow. Permit UDP 41641 between "
                                                   "this Mac's subnet and the host's subnet"))
    elif " via " in text:
        out.append(Check("ok", "Tailscale path", "direct"))
    else:
        out.append(Check("WARN", "Tailscale path", f"`tailscale ping {cfg.gw_hostname}` got no answer"))
    return out


def isolation_checks(cfg: Config, runner: Runner, api=None) -> list[Check]:
    """docs/setup.md step 8, the controlled pair. The LAN target is the Proxmox
    host itself: it is on the LAN, and it answers on 8006."""
    from . import cli
    lan = urllib.parse.urlsplit(cfg.pve_api).hostname or ""
    try:
        lan_ip = socket.gethostbyname(lan)
    except OSError:
        return [Check("FAIL", "isolation", f"cannot resolve {lan}")]
    if not ipaddress.ip_address(lan_ip).is_private:
        return [Check("WARN", "isolation", f"{lan_ip} is not a private address; the LAN probe proves nothing")]
    ts = _tailscale()
    mac_ts = runner.run([ts, "ip", "-4"], check=False).stdout.strip().splitlines() if ts else []
    boxes = {"agent": "doctor-a", "personal": "doctor-p"}
    out = []
    try:
        for profile, name in boxes.items():
            argv = ["new", name, "--profile", profile, "--no-herdr", "--no-claude", "--ttl", "1"]
            argv += ["--no-remote-control"] if profile == "personal" else []
            if cli.main(argv, runner=runner, api=api) != 0:
                return out + [Check("FAIL", "isolation", f"`sbx new {name}` failed")]

        def probe(name: str, command: str) -> bool:
            return Vm(cfg, runner, f"sbx-{name}").run(command, check=False).code == 0

        web = "curl -sS -m5 -o /dev/null https://example.com"
        host = f"curl -sk -m5 -o /dev/null https://{lan_ip}:8006/"
        expect = [
            ("agent reaches the internet", boxes["agent"], web, True),
            ("personal reaches the LAN", boxes["personal"], host, True),
            ("agent cannot reach the LAN", boxes["agent"], host, False),
        ]
        for ip in mac_ts[:1]:
            expect += [("agent cannot reach this Mac on the tailnet", boxes["agent"], f"ping -c1 -W2 {ip}", False),
                       ("personal cannot reach this Mac on the tailnet", boxes["personal"], f"ping -c1 -W2 {ip}", False)]
        for label, name, command, want in expect:
            got = probe(name, command)
            out.append(Check("ok" if got == want else "FAIL", label, "" if got == want else
                             ("it can" if got else "it cannot") + f": {command}"))
        if all(c.status == "FAIL" for c in out[:2]):
            out.append(Check("FAIL", "isolation", "no probe reached anything: the network is broken, "
                                                  "so the blocked probes prove nothing"))
    finally:
        for name in boxes.values():
            cli.main(["rm", name, "-y"], runner=runner, api=api)
    return out


def report(checks: list[Check]) -> int:
    width = max((len(c.name) for c in checks), default=0)
    for c in checks:
        print(f"  {c.status:<4}  {c.name:<{width}}  {c.detail}".rstrip())
    fails = sum(c.status == "FAIL" for c in checks)
    warns = sum(c.status == "WARN" for c in checks)
    print(f"\n{fails} failed, {warns} warnings, {len(checks) - fails - warns} ok")
    return 1 if fails else 0


def run(cfg: Config, runner: Runner, api=None, isolation: bool = False) -> int:
    checks = mac_checks(cfg, runner)
    if not any(c.status == "FAIL" and c.name == "config.toml" for c in checks):
        checks += api_checks(cfg, runner, api)
    checks += network_checks(cfg, runner)
    if isolation:
        if any(c.status == "FAIL" for c in checks):
            checks.append(Check("FAIL", "isolation", "not run: correct the failures above first"))
        else:
            print("making two probe sandboxes; this takes about two minutes")
            checks += isolation_checks(cfg, runner, api)
    return report(checks)


def cmd_doctor(args, cfg: Config, runner: Runner, api=None) -> int:
    return run(cfg, runner, api, isolation=args.isolation)
