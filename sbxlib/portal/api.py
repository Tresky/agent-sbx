"""The portal's JSON API: one function per route.

A read calls sbxlib directly and returns structured data. A change that takes
time, or that the CLI already does with its own checks, is a job (jobs.py):
the portal runs `sbx <command>` and the Activity page keeps its output. A
command that needs the host's root password or a browser sign-in opens in
Terminal, where the user types the answer: the portal never holds the host
password.

Nothing here returns a secret. A token's presence is read from the keychain
without its value (`security find-generic-password` without -w), a binding's
literal value is never read, and the secrets that the user types (a git
token, a Claude token, a sign-in code) go to the child's stdin only.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from .. import claudetoken, cli, doctor, gittoken, herdr as herdr_mod, names, remotecontrol
from .. import projects as projects_mod
from .. import templates as templates_mod
from ..config import (_ENV_MAP, DEFAULTS_ENV, REPO_ROOT, Config, ConfigError, load as load_config,
                      local_conf_path, parse_env_file, state_dir)
from ..inputs import InputError, load_bindings, preview
from ..manifest import MANIFEST_PATH, Manifest, ManifestError
from ..pve import HttpApi, Pve, PveError, Sandbox
from ..run import CommandError, Runner
from ..vm import Vm, VmError
from .jobs import Jobs


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def bad(message: str) -> HttpError:
    return HttpError(400, message)


# The errors that the CLI prints as `sbx: error:`. The portal shows them the same way.
USER_ERRORS = (ConfigError, ManifestError, InputError, names.NameError_, PveError, projects_mod.ProjectError,
               templates_mod.TemplateError)
REMOTE_ERRORS = (VmError, CommandError)

_INPUT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_MODE_RE = re.compile(r"^[A-Za-z]{1,32}$")
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")   # Proxmox's rule for a snapshot name
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_BOX_RE = re.compile(r"^[a-z0-9-]{1,63}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/+-]{1,200}$")


class Context:
    """What each route needs: the settings, the Proxmox API, the jobs."""

    def __init__(self, jobs: Jobs, runner: Runner | None = None, api=None):
        self.jobs = jobs
        self.runner = runner or Runner()
        self._api = api                 # a test seam, as in cli.main
        self._pve: tuple[tuple, Pve] | None = None

    def cfg(self) -> Config:
        return load_config()

    def _stamp(self) -> tuple:
        out = []
        for p in (state_dir() / "config.toml", local_conf_path(), DEFAULTS_ENV):
            try:
                out.append(p.stat().st_mtime_ns)
            except OSError:
                out.append(0)
        return tuple(out)

    def pve(self, cfg: Config | None = None) -> Pve:
        """One Pve for as long as the settings do not change, so the token
        command runs once and not on each request."""
        stamp = self._stamp()
        if self._pve is None or self._pve[0] != stamp:
            cfg = cfg or self.cfg()
            self._pve = (stamp, Pve(cfg, self._api or HttpApi(cfg, self.runner)))
        return self._pve[1]


# --- helpers ------------------------------------------------------------------

def _sandbox_name(raw: str) -> str:
    return names.hostname(raw)


def _box(ctx: Context, raw: str) -> Sandbox:
    hostname = _sandbox_name(raw)
    box = ctx.pve().find(hostname)
    if box is None:
        raise HttpError(404, f"no sandbox named {hostname}")
    return box


def _vm_path(box: Sandbox, tail: str = "") -> str:
    return f"/nodes/{box.node}/qemu/{box.vmid}{tail}"


def _int(body: dict, key: str, low: int, high: int) -> int | None:
    value = body.get(key)
    if value in (None, ""):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise bad(f"{key} must be a whole number") from None
    if not low <= value <= high:
        raise bad(f"{key} must be from {low} to {high}")
    return value


def _str(body: dict, key: str, pattern: re.Pattern | None = None, required: bool = False) -> str:
    value = body.get(key)
    if value in (None, ""):
        if required:
            raise bad(f"{key} is missing")
        return ""
    if not isinstance(value, str) or "\n" in value or "\x00" in value:
        raise bad(f"{key} must be one line of text")
    if pattern is not None and not pattern.match(value):
        raise bad(f"{key}: {value!r} is not valid")
    return value


def _secret(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value.strip()):
        raise bad(f"the {key} is empty or has whitespace in it")
    return value.strip()


def _names_list(body: dict, key: str, pattern: re.Pattern) -> list[str]:
    value = body.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) and pattern.match(v) for v in value):
        raise bad(f"{key} must be a list of names")
    return value


def _job(job) -> dict:
    return {"job": job.summary()}


def _https(hostname: str) -> bool:
    return (state_dir() / "certs" / hostname / "cert.pem").exists()


def _keychain_has(ctx: Context, service: str, account: str = "sbx") -> bool:
    # No -w: the item's attributes, not its secret.
    return ctx.runner.run(["security", "find-generic-password", "-s", service, "-a", account],
                          check=False).code == 0


def _box_dict(cfg: Config, box: Sandbox, resource: dict | None = None) -> dict:
    today = dt.date.today()
    exp = box.expires
    r = resource or {}
    return {
        "name": box.hostname[len(names.PREFIX):], "hostname": box.hostname, "vmid": box.vmid, "node": box.node,
        "status": box.status, "profile": box.profile, "template": box.template, "project": box.project,
        "expires": exp.isoformat() if exp else None, "expired": bool(exp and exp < today),
        "fqdn": cfg.fqdn(box.hostname), "https": _https(box.hostname), "tags": list(box.tags),
        "cpu": r.get("cpu"), "maxcpu": r.get("maxcpu"), "mem": r.get("mem"), "maxmem": r.get("maxmem"),
        "disk": r.get("disk"), "maxdisk": r.get("maxdisk"), "uptime": r.get("uptime"),
        "netin": r.get("netin"), "netout": r.get("netout"), "lock": r.get("lock"),
    }


def _sandbox_rows(ctx: Context, cfg: Config) -> tuple[list[dict], list[dict]]:
    """(the sandboxes, the raw resources) from one API call."""
    pve = ctx.pve(cfg)
    resources = pve.resources()
    by_id = {int(r["vmid"]): r for r in resources if "vmid" in r}
    return [_box_dict(cfg, b, by_id.get(b.vmid)) for b in pve.sandboxes(resources)], resources


def _claude_status(ctx: Context) -> dict:
    stored = _keychain_has(ctx, claudetoken.SERVICE, claudetoken.ACCOUNT)
    end = claudetoken.expires()
    return {"stored": stored, "expires": end.isoformat() if end else None,
            "warning": claudetoken.expiry_warning() if stored else None}


def _template_states(cfg: Config, built: dict, boxes: list[dict]) -> tuple[list[dict], str | None]:
    try:
        defs = cli._definitions()
        defs_error = None
    except ConfigError as exc:
        defs, defs_error = {}, str(exc)
    rows = []
    for name in sorted(set(defs) | set(built)):
        defn, versions = defs.get(name), built.get(name, [])
        rows.append({
            "name": name, "default": name == cfg.default_template,
            "source": None if defn is None else ("local" if defn.local else "shared"),
            "state": cli._state(defn, versions),
            "description": defn.description if defn else "",
            "components": list(defn.components) if defn else [],
            "versions": [{"vmid": v.vmid, "vm_name": v.vm_name, "node": v.node, "fingerprint": v.fingerprint}
                         for v in versions],
            "sandboxes": [b["name"] for b in boxes if b["template"] == name],
        })
    return rows, defs_error


# --- overview -----------------------------------------------------------------

def overview(ctx: Context, req) -> dict:
    out: dict = {"errors": {}}
    try:
        cfg = ctx.cfg()
    except ConfigError as exc:
        return {"config_error": str(exc), "checks": [], "claude": _claude_status(ctx),
                "jobs": [j.summary() for j in ctx.jobs.all()[:8]], "errors": {}}
    out["config"] = {"pve_api": cfg.pve_api, "domain": cfg.domain, "default_profile": cfg.default_profile,
                     "default_template": cfg.default_template, "agent_ttl_days": cfg.agent_ttl_days,
                     "cores": cfg.cores, "memory_mb": cfg.memory_mb, "gpu_mapping": cfg.gpu_mapping}
    out["checks"] = [dataclasses.asdict(c) for c in doctor.mac_checks(cfg, ctx.runner)]
    boxes, built = [], {}
    try:
        pve = ctx.pve(cfg)
        version = pve.api("GET", "/version") or {}
        out["pve_version"] = version.get("version")
        boxes, resources = _sandbox_rows(ctx, cfg)
        built = pve.templates(resources)
    except Exception as exc:  # noqa: BLE001 - the page shows any failure to reach the host
        out["errors"]["pve"] = str(exc)
    out["sandboxes"] = boxes
    out["templates"], defs_error = _template_states(cfg, built, boxes)
    if defs_error:
        out["errors"]["templates"] = defs_error
    out["claude"] = _claude_status(ctx)
    out["projects"] = len(projects_mod.load())
    out["jobs"] = [j.summary() for j in ctx.jobs.all()[:8]]
    out["herdr"] = herdr_mod.available()
    return out


# --- sandboxes ----------------------------------------------------------------

def sandboxes(ctx: Context, req) -> dict:
    cfg = ctx.cfg()
    rows, _ = _sandbox_rows(ctx, cfg)
    return {"sandboxes": rows, "domain": cfg.domain}


def sandbox(ctx: Context, req, name: str) -> dict:
    cfg = ctx.cfg()
    pve = ctx.pve(cfg)
    resources = pve.resources()
    box = next((b for b in pve.sandboxes(resources) if b.hostname == _sandbox_name(name)), None)
    if box is None:
        raise HttpError(404, f"no sandbox named {names.hostname(name)}")
    resource = next((r for r in resources if int(r.get("vmid", -1)) == box.vmid), {})
    out = {"sandbox": _box_dict(cfg, box, resource), "errors": {}}

    def attempt(key, fn):
        try:
            out[key] = fn()
        except PveError as exc:
            out[key] = None
            out["errors"][key] = str(exc)

    attempt("current", lambda: pve.api("GET", _vm_path(box, "/status/current")))
    attempt("config", lambda: pve.api("GET", _vm_path(box, "/config")))
    attempt("snapshots", lambda: [s for s in pve.api("GET", _vm_path(box, "/snapshot")) or []
                                  if s.get("name") != "current"])
    timeframe = req.query.get("timeframe", "hour")
    if timeframe not in ("hour", "day", "week"):
        raise bad("timeframe must be hour, day or week")
    keys = ("time", "cpu", "maxcpu", "mem", "maxmem", "netin", "netout", "diskread", "diskwrite")
    attempt("rrd", lambda: [{k: p.get(k) for k in keys}
                            for p in pve.api("GET", _vm_path(box, "/rrddata"),
                                             {"timeframe": timeframe, "cf": "AVERAGE"}) or []])
    out["address"] = pve.guest_ipv4(box.node, box.vmid) if box.status == "running" else None
    out["remote_control_allowed"] = box.profile == remotecontrol.ALLOWED_PROFILE
    out["gpu_mapping"] = cfg.gpu_mapping
    out["herdr"] = herdr_mod.available()
    out["running_job"] = next((j.summary() for j in ctx.jobs.running() if j.target == f"sandbox:{box.hostname}"), None)
    return out


def sandbox_tasks(ctx: Context, req, name: str) -> dict:
    box = _box(ctx, name)
    try:
        tasks = ctx.pve().api("GET", f"/nodes/{box.node}/tasks", {"vmid": box.vmid, "limit": 30}) or []
    except PveError as exc:
        return {"tasks": [], "error": str(exc)}
    return {"tasks": tasks}


def task_log(ctx: Context, req, node: str, upid: str) -> dict:
    if not re.match(r"^[A-Za-z0-9-]+$", node) or not upid.startswith("UPID:"):
        raise bad("not a task")
    import urllib.parse
    lines = ctx.pve().api("GET", f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/log",
                          {"limit": 1000}) or []
    return {"lines": [l.get("t", "") for l in lines]}


def _vm(ctx: Context, box: Sandbox) -> Vm:
    return Vm(ctx.cfg(), ctx.runner, box.hostname)


def _ssh(ctx: Context, box: Sandbox, command: str, timeout_hint: str = "") -> str:
    if box.status != "running":
        raise HttpError(409, f"{box.hostname} is {box.status}; start it first")
    done = _vm(ctx, box).run(command, check=False)
    if done.code == 255:
        raise HttpError(502, f"{box.hostname} does not answer on SSH: {done.stderr.strip()[-300:]}")
    return done.stdout


# Each port the sandbox listens on. sudo -n: the dev user has sudo with no
# password, and the process names of other users need it.
_PORTS = "sudo -n ss -ltnpH 2>/dev/null || ss -ltnH"
_QUIET_PORTS = {22, 53, 2019, 5355}   # ssh, the resolver, Caddy's admin API, LLMNR


def ports(ctx: Context, req, name: str) -> dict:
    box = _box(ctx, name)
    cfg = ctx.cfg()
    found: dict[int, dict] = {}
    for line in _ssh(ctx, box, _PORTS).splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]
        addr, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        addr = addr.strip("[]").split("%")[0]
        proc = ""
        if m := re.search(r'users:\(\("([^"]+)"', line):
            proc = m.group(1)
        entry = found.setdefault(int(port), {"port": int(port), "addresses": [], "process": "", "mirrored": False})
        entry["addresses"].append(addr)
        if proc in ("caddy", "python3") and addr not in ("127.0.0.1", "::1", "*", "0.0.0.0", "::"):
            entry["mirrored"] = True   # the port mirror's own listener on the routed address
        elif proc and not entry["process"]:
            entry["process"] = proc
    out = []
    https = _https(box.hostname)
    for port, e in sorted(found.items()):
        loopback = all(a in ("127.0.0.1", "::1", "127.0.0.53", "127.0.0.54") for a in e["addresses"])
        wildcard = any(a in ("*", "0.0.0.0", "::") for a in e["addresses"])
        # A loopback port goes through the mirror, with TLS when the sandbox has
        # a certificate; a wildcard port (Docker) is reachable as it is.
        scheme = "https" if https and not wildcard else "http"
        e.update(system=port in _QUIET_PORTS, reachable=wildcard or e["mirrored"] or loopback,
                 url=f"{scheme}://{cfg.fqdn(box.hostname)}:{port}")
        out.append(e)
    return {"ports": out}


_SECTIONS = r"""
s() { printf '\n==sbx:%s\n' "$1"; }
s template; cat /etc/sbx/template 2>/dev/null
s uptime; uptime
s os; . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME"; uname -r
s disk; df -h / | tail -n 1
s memory; free -m | sed -n 2p
s load; cat /proc/loadavg
s claude; test -f ~/.config/sbx/claude.env && echo signed-in || echo none
s repos; for d in ~/code/*/; do [ -d "$d/.git" ] || continue; (cd "$d" && printf '%s\t%s\t%s\t%s\n' "$(basename "$d")" "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" "$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')" "$(git log -1 --format='%h %s' 2>/dev/null)"); done
s docker; docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null
s services; systemctl --failed --no-legend --plain 2>/dev/null | head -n 20
s recipe; tail -n 1 ~/.local/state/sbx/recipe.log 2>/dev/null
"""


def system(ctx: Context, req, name: str) -> dict:
    box = _box(ctx, name)
    text = _ssh(ctx, box, _SECTIONS)
    sections: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("==sbx:"):
            current = line[6:].strip()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
    repos = []
    for line in sections.get("repos", "").splitlines():
        cols = line.split("\t")
        if len(cols) == 4:
            repos.append({"name": cols[0], "branch": cols[1], "changed": int(cols[2] or 0), "last": cols[3]})
    containers = [dict(zip(("name", "image", "status"), l.split("\t"))) for l in sections.get("docker", "").splitlines()
                  if l.count("\t") == 2]
    return {"sections": {k: v.strip() for k, v in sections.items()}, "repos": repos, "containers": containers}


# The logs that the Logs tab offers. A fixed list: the request names one, and
# never a command or a path.
LOGS = {
    "recipe": ("The recipe", "tail -n {n} ~/.local/state/sbx/recipe.log 2>&1"),
    "remote-control": ("Remote Control", "tail -n {n} ~/.local/state/sbx/remote-control.log 2>&1"),
    "cloud-init": ("cloud-init", "sudo -n tail -n {n} /var/log/cloud-init-output.log 2>&1"),
    "system": ("System journal", "sudo -n journalctl -n {n} --no-pager -o short-iso 2>&1"),
    "mirror": ("Port mirror", "sudo -n journalctl -u sbx-mirror -n {n} --no-pager -o short-iso 2>&1"),
    "caddy": ("Caddy", "sudo -n journalctl -u caddy -n {n} --no-pager -o short-iso 2>&1"),
    "docker": ("Docker", "sudo -n journalctl -u docker -n {n} --no-pager -o short-iso 2>&1"),
    "auth": ("Logins", "sudo -n journalctl _COMM=sshd -n {n} --no-pager -o short-iso 2>&1"),
}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


def log_names(ctx: Context, req) -> dict:
    return {"logs": [{"id": k, "title": v[0]} for k, v in LOGS.items()]}


def log(ctx: Context, req, name: str, which: str) -> dict:
    if which not in LOGS:
        raise HttpError(404, f"no log {which!r}")
    box = _box(ctx, name)
    try:
        n = max(10, min(5000, int(req.query.get("lines", "300"))))
    except ValueError:
        raise bad("lines must be a number") from None
    text = _ssh(ctx, box, LOGS[which][1].format(n=n))
    return {"title": LOGS[which][0], "text": _ANSI.sub("", text)}


POWER = {"start": "Start", "shutdown": "Shut down", "stop": "Stop", "reboot": "Reboot"}


def power(ctx: Context, req, name: str) -> dict:
    action = req.body.get("action")
    if action not in POWER:
        raise bad(f"action must be one of {', '.join(POWER)}")
    box = _box(ctx, name)
    pve = ctx.pve()

    def work(log):
        log(f"{POWER[action]} {box.hostname} (VM {box.vmid})")
        # shutdown asks the guest; stop pulls the power. Both wait for the task.
        pve._wait(box.node, pve.api("POST", _vm_path(box, f"/status/{action}")))
        log("done")

    return _job(ctx.jobs.run_fn(work, f"{POWER[action]} {box.hostname}", f"proxmox: {action} VM {box.vmid}",
                                f"sandbox:{box.hostname}"))


def snapshot_take(ctx: Context, req, name: str) -> dict:
    box = _box(ctx, name)
    label = _str(req.body, "label", _LABEL_RE) or "clean"
    return _job(ctx.jobs.run_sbx(["snap", box.hostname[4:], label], f"Snapshot {box.hostname} as {label}",
                                 f"sandbox:{box.hostname}"))


def snapshot_rollback(ctx: Context, req, name: str, label: str) -> dict:
    box = _box(ctx, name)
    if not _LABEL_RE.match(label):
        raise bad("not a snapshot name")
    return _job(ctx.jobs.run_sbx(["rollback", box.hostname[4:], label], f"Roll back {box.hostname} to {label}",
                                 f"sandbox:{box.hostname}"))


def snapshot_delete(ctx: Context, req, name: str, label: str) -> dict:
    box = _box(ctx, name)
    if not _LABEL_RE.match(label):
        raise bad("not a snapshot name")
    pve = ctx.pve()

    def work(log):
        log(f"deleting snapshot {label} of {box.hostname}")
        pve._wait(box.node, pve.api("DELETE", _vm_path(box, f"/snapshot/{label}")))
        log("done")

    return _job(ctx.jobs.run_fn(work, f"Delete snapshot {label} of {box.hostname}",
                                f"proxmox: delete snapshot {label} of VM {box.vmid}", f"sandbox:{box.hostname}"))


def expiry(ctx: Context, req, name: str) -> dict:
    """A new expiry date, in the sbx-exp- tag, or none."""
    box = _box(ctx, name)
    body = req.body
    if body.get("never"):
        when = None
    elif body.get("days") not in (None, ""):
        when = dt.date.today() + dt.timedelta(days=_int(body, "days", 0, 3650) or 0)
    else:
        try:
            when = dt.date.fromisoformat(_str(body, "date", required=True))
        except ValueError:
            raise bad("date must be YYYY-MM-DD") from None
    tags = [t for t in box.tags if not t.startswith("sbx-exp-")] + ([f"sbx-exp-{when:%Y%m%d}"] if when else [])
    ctx.pve().api("PUT", _vm_path(box), {"tags": ";".join(tags)})
    text = f"expires {when}" if when else "never expires"
    ctx.jobs.record(f"{box.hostname} {text}", f"proxmox: tags of VM {box.vmid}", f"sandbox:{box.hostname}")
    return {"expires": when.isoformat() if when else None}


def destroy(ctx: Context, req, name: str) -> dict:
    box = _box(ctx, name)
    return _job(ctx.jobs.run_sbx(["rm", box.hostname[4:], "-y"], f"Destroy {box.hostname}", f"sandbox:{box.hostname}"))


def herdr_add(ctx: Context, req, name: str) -> dict:
    box = _box(ctx, name)
    return _job(ctx.jobs.run_sbx(["herdr", box.hostname[4:]], f"Add {box.hostname} to herdr", f"sandbox:{box.hostname}"))


def layout(ctx: Context, req, name: str) -> dict:
    box = _box(ctx, name)
    args = ["layout", box.hostname[4:]] + (["--replace"] if req.body.get("replace") else []) \
        + (["--no-run"] if req.body.get("no_run") else [])
    return _job(ctx.jobs.run_sbx(args, f"Build the herdr panes of {box.hostname}", f"sandbox:{box.hostname}"))


def gpu(ctx: Context, req, name: str) -> dict:
    action = req.body.get("action")
    if action not in ("attach", "detach"):
        raise bad("action must be attach or detach")
    box = _box(ctx, name)
    return _job(ctx.jobs.run_sbx(["gpu", action, box.hostname[4:]], f"GPU {action}: {box.hostname}",
                                 f"sandbox:{box.hostname}"))


def gpu_status(ctx: Context, req) -> dict:
    cfg = ctx.cfg()
    if not cfg.gpu_mapping:
        return {"mapping": "", "holder": None}
    holder = ctx.pve(cfg).gpu_holder()
    return {"mapping": cfg.gpu_mapping, "holder": {"vmid": holder[0], "name": holder[1]} if holder else None}


def new_sandbox(ctx: Context, req) -> dict:
    """`sbx new`, with each field checked here first, so a typing mistake
    fails in the form and not in the Activity log."""
    b = req.body
    hostname = _sandbox_name(_str(b, "name", required=True))
    args = ["new", hostname[4:]]
    profile = _str(b, "profile")
    if profile:
        if profile not in ("agent", "personal"):
            raise bad("profile must be agent or personal")
        args.append(f"--profile={profile}")
    if tpl := _str(b, "template", templates_mod.NAME_RE):
        args.append(f"--template={tpl}")
    if project := _str(b, "project"):
        # --opt=value: a value that starts with "-" stays a value.
        args.append(f"--project={project}")
        if branch := _str(b, "branch", _BRANCH_RE):
            args.append(f"--branch={branch}")
        if source := _str(b, "from"):
            args.append(f"--from={source}")
        args += [f"--with={n}" for n in _names_list(b, "with", _INPUT_RE)]
        args += [f"--without={n}" for n in _names_list(b, "without", _INPUT_RE)]
    for key, low, high, flag in (("cores", 1, 256, "--cores"), ("memory", 256, 4194304, "--memory"),
                                 ("disk", 1, 65536, "--disk"), ("ttl", 0, 3650, "--ttl")):
        value = _int(b, key, low, high)
        if value is not None:
            args.append(f"{flag}={value}")
    for key in ("gpu", "no_herdr", "no_claude", "no_remote_control"):
        if b.get(key):
            args.append("--" + key.replace("_", "-"))
    if mode := _str(b, "remote_control_mode", _MODE_RE):
        args.append(f"--remote-control-mode={mode}")
    return _job(ctx.jobs.run_sbx(args, f"New sandbox {hostname}", f"sandbox:{hostname}"))


def gc(ctx: Context, req) -> dict:
    return _job(ctx.jobs.run_sbx(["gc", "-y"], "Destroy the expired sandboxes", "gc"))


# --- Remote Control -----------------------------------------------------------

def _rc(ctx: Context, name: str) -> tuple[Sandbox, Vm, str, str]:
    box = _box(ctx, name)
    remotecontrol.check_profile(box.profile, box.hostname)
    project = cli.Project(box.project, "", None, None, None) if box.project else None
    directory = cli._rc_directory(project)
    label = box.hostname + (f" · {box.project}" if box.project else "")
    return box, _vm(ctx, box), directory, label


def rc_status(ctx: Context, req, name: str) -> dict:
    box, vm, _, _ = _rc(ctx, name)
    if box.status != "running":
        return {"status": box.status, "signed_in": None, "mode": ctx.cfg().remote_control_mode}
    return {"status": remotecontrol.status(vm), "signed_in": remotecontrol.logged_in(vm),
            "mode": ctx.cfg().remote_control_mode}


def rc_action(ctx: Context, req, name: str) -> dict:
    box, vm, directory, label = _rc(ctx, name)
    action = req.body.get("action")
    target = f"sandbox:{box.hostname}"
    if action == "login":
        # Step one of the sign-in: the URL to open. The code comes back in step two.
        if remotecontrol.logged_in(vm):
            return {"signed_in": True}
        return {"url": remotecontrol.start_login(vm, directory)}
    if action == "code":
        code = _secret(req.body, "code")
        mode = _str(req.body, "mode", _MODE_RE) or ctx.cfg().remote_control_mode or "acceptEdits"

        def finish(log):
            log("typing the code into the waiting sign-in")
            remotecontrol.finish_login(vm, code)
            log(f"{box.hostname} is signed in to claude.ai; starting the server")
            log(f"Remote Control server: {remotecontrol.enable(vm, directory, label, mode)}")

        return _job(ctx.jobs.run_fn(finish, f"Sign {box.hostname} in to Remote Control",
                                    "remote control: sign-in", target))
    if action == "enable":
        mode = _str(req.body, "mode", _MODE_RE) or ctx.cfg().remote_control_mode or "acceptEdits"
        return _job(ctx.jobs.run_fn(lambda log: log(f"Remote Control server: {remotecontrol.enable(vm, directory, label, mode)}"),
                                    f"Start Remote Control in {box.hostname}", f"remote control: start ({mode})", target))
    if action == "off":
        return _job(ctx.jobs.run_fn(lambda log: (remotecontrol.disable(vm), log("stopped and disabled; the sign-in stays")),
                                    f"Stop Remote Control in {box.hostname}", "remote control: off", target))
    raise bad("action must be login, code, enable or off")


# --- Terminal -----------------------------------------------------------------

# The commands that need a terminal: the host's root password, a browser
# sign-in, or an interactive shell. The portal opens them in Terminal.app.
_TERMINAL = {"ssh", "setup", "template", "claude-token", "remote-control", "herdr", "doctor", "git-token"}


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def terminal_command(args: list[str]) -> str:
    return shlex.join([str(REPO_ROOT / "bin" / "sbx"), *args])


def terminal(ctx: Context, req) -> dict:
    args = req.body.get("args")
    if not (isinstance(args, list) and args and all(isinstance(a, str) and a and "\n" not in a and "\x00" not in a
                                                        for a in args)):
        raise bad("args must be a list of words")
    if args[0] not in _TERMINAL:
        raise bad(f"`sbx {args[0]}` does not open in Terminal from the portal")
    command = terminal_command(args)
    script = f'tell application "Terminal"\nactivate\ndo script {_applescript_string(command)}\nend tell'
    done = ctx.runner.run(["osascript", "-e", script], check=False)
    if done.code != 0:
        raise HttpError(500, f"Terminal did not open: {done.stderr.strip()}")
    ctx.jobs.record(f"Opened in Terminal: sbx {args[0]}", "sbx " + shlex.join(args))
    return {"command": command}


# --- templates ----------------------------------------------------------------

def templates(ctx: Context, req) -> dict:
    cfg = ctx.cfg()
    out: dict = {"errors": {}}
    boxes, built = [], {}
    try:
        boxes, resources = _sandbox_rows(ctx, cfg)
        built = ctx.pve(cfg).templates(resources)
    except Exception as exc:  # noqa: BLE001
        out["errors"]["pve"] = str(exc)
    out["templates"], defs_error = _template_states(cfg, built, boxes)
    if defs_error:
        out["errors"]["definitions"] = defs_error
    out["components"] = components(ctx, req)["components"]
    out["default_template"] = cfg.default_template
    return out


def template(ctx: Context, req, name: str) -> dict:
    if not templates_mod.NAME_RE.match(name):
        raise HttpError(404, "no such template")
    cfg = ctx.cfg()
    out: dict = {"name": name, "errors": {}, "default": name == cfg.default_template}
    paths = templates_mod.definition_paths()
    shared, local = templates_mod.definition_dirs()
    out["shadows_shared"] = (local / f"{name}.toml").is_file() and (shared / f"{name}.toml").is_file()
    if name in paths:
        path = paths[name]
        out["definition"] = {"path": str(path.relative_to(REPO_ROOT)), "text": path.read_text(),
                             "local": path.parent.name == "local"}
        try:
            defn = templates_mod.load(name)
            out["parsed"] = {
                "description": defn.description, "components": list(defn.components), "apt": list(defn.apt),
                "cores": defn.cores, "memory_mb": defn.memory_mb, "disk_gb": defn.disk_gb,
                "image_url": defn.image_url, "settings": templates_mod.effective_settings(defn),
                "build_env": templates_mod.build_env(defn), "fingerprint": templates_mod.fingerprint(defn),
                "derived": templates_mod.derived(name),
            }
        except (templates_mod.TemplateError, OSError) as exc:
            out["errors"]["definition"] = str(exc)
    else:
        out["definition"] = None
    try:
        boxes, resources = _sandbox_rows(ctx, cfg)
        built = ctx.pve(cfg).templates(resources).get(name, [])
        out["versions"] = [{"vmid": v.vmid, "vm_name": v.vm_name, "node": v.node, "fingerprint": v.fingerprint,
                            "sandboxes": [b["name"] for b in boxes if b["template"] == name]} for v in built]
        out["sandboxes"] = [b for b in boxes if b["template"] == name]
        try:
            defn_obj = templates_mod.load(name) if name in paths else None
        except templates_mod.TemplateError:
            defn_obj = None
        out["state"] = cli._state(defn_obj, built)
    except Exception as exc:  # noqa: BLE001
        out["errors"]["pve"] = str(exc)
        out["versions"], out["sandboxes"], out["state"] = [], [], "?"
    if out["definition"] is None and not out["versions"] and "pve" not in out["errors"]:
        raise HttpError(404, f"no template '{name}'")
    return out


def template_save(ctx: Context, req, name: str) -> dict:
    """Save a LOCAL definition. It must parse first: a broken file would stop
    every `sbx template` command."""
    if not templates_mod.NAME_RE.match(name):
        raise bad("not a template name")
    text = req.body.get("text")
    if not isinstance(text, str) or len(text) > 200_000:
        raise bad("text must be the definition")
    _, local = templates_mod.definition_dirs()
    path = local / f"{name}.toml"
    if not path.is_file():
        raise bad(f"{name} has no local definition; a shared one is edited in git. "
                  "Make a local copy first, which then hides the shared one")
    try:
        templates_mod.parse(text, name, path)
    except tomllib.TOMLDecodeError as exc:
        raise bad(f"not valid TOML: {exc}") from None
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    ctx.jobs.record(f"Saved the definition of template {name}", f"write {path.relative_to(REPO_ROOT)}",
                    f"template:{name}")
    return {"saved": True, "fingerprint": templates_mod.fingerprint(templates_mod.load(name))}


def template_new(ctx: Context, req) -> dict:
    import argparse
    name = _str(req.body, "name", templates_mod.NAME_RE, required=True)
    source = _str(req.body, "from", templates_mod.NAME_RE)
    cli.cmd_template(argparse.Namespace(action="new", name=name, from_name=source or None), ctx.cfg(), ctx.runner)
    ctx.jobs.record(f"New template definition {name}" + (f" from {source}" if source else ""),
                    f"sbx template new {name}" + (f" --from {source}" if source else ""), f"template:{name}")
    return {"name": name}


def template_delete_definition(ctx: Context, req, name: str) -> dict:
    if not templates_mod.NAME_RE.match(name):
        raise bad("not a template name")
    _, local = templates_mod.definition_dirs()
    path = local / f"{name}.toml"
    if not path.is_file():
        raise bad(f"{name} has no local definition; the portal removes local definitions only")
    path.unlink()
    ctx.jobs.record(f"Removed the local definition of template {name}", f"rm {path.relative_to(REPO_ROOT)}",
                    f"template:{name}")
    return {"removed": True}


def template_export(ctx: Context, req, name: str):
    if not templates_mod.NAME_RE.match(name):
        raise bad("not a template name")
    return ("text/plain; charset=utf-8", templates_mod.export_bundle(name), f"{name}.sbx-template.toml")


def _import_plan(body: dict):
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise bad("paste or choose a template file")
    bundle = templates_mod.read_bundle(text)
    as_name = _str(body, "as", templates_mod.NAME_RE)
    return bundle, templates_mod.plan_import(bundle, as_name, bool(body.get("force")))


def _plan_digest(plan) -> str:
    h = hashlib.sha256()
    for path in sorted(plan.writes):
        h.update(str(path).encode() + b"\0" + plan.writes[path].encode() + b"\0")
    return h.hexdigest()


def template_import(ctx: Context, req) -> dict:
    """Two steps. Without `apply`: the plan, with the full text of each file.
    With `apply` and the plan's digest: the files, exactly as they were shown.
    A component runs as root in the build, so nothing is written unseen."""
    bundle, plan = _import_plan(req.body)
    digest = _plan_digest(plan)
    out = {"name": plan.name, "exported_as": bundle.name, "problems": plan.problems, "warnings": plan.warnings,
           "same": plan.same, "digest": digest,
           "writes": [{"path": str(p.relative_to(REPO_ROOT)), "text": t, "component": p.suffix == ".sh"}
                      for p, t in sorted(plan.writes.items())]}
    if not req.body.get("apply"):
        return out
    if req.body.get("digest") != digest:
        raise HttpError(409, "the files differ from the ones you reviewed; review them again")
    if plan.problems:
        raise bad("nothing imported: " + "; ".join(plan.problems))
    written = templates_mod.apply_import(plan)
    ctx.jobs.record(f"Imported template {plan.name}", f"sbx template import (as {plan.name})", f"template:{plan.name}",
                    [f"wrote {p.relative_to(REPO_ROOT)}" for p in written])
    out["written"] = [str(p.relative_to(REPO_ROOT)) for p in written]
    return out


def components(ctx: Context, req) -> dict:
    out = []
    for name, path in templates_mod.components().items():
        out.append({"name": name, "local": path.parent.name == "local", "description": templates_mod.describe(path),
                    "requires": templates_mod.requires(path)})
    return {"components": out}


def component(ctx: Context, req, name: str) -> dict:
    if not re.match(r"^[a-z][a-z0-9-]{0,31}$", name):
        raise HttpError(404, "no such component")
    path = templates_mod.component_path(name)
    if path is None:
        raise HttpError(404, f"no component {name}")
    users = []
    for tname, tpath in templates_mod.definition_paths().items():
        if name in templates_mod._components_of(tpath):
            users.append(tname)
    return {"name": name, "path": str(path.relative_to(REPO_ROOT)), "local": path.parent.name == "local",
            "text": path.read_text(), "description": templates_mod.describe(path),
            "requires": templates_mod.requires(path), "used_by": users}


def versions(ctx: Context, req) -> dict:
    done = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "sbx"), "versions"], capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=180, env=dict(os.environ, NO_COLOR="1"))
    return {"text": (done.stdout + done.stderr).decode(errors="replace"), "code": done.returncode}


def versions_write(ctx: Context, req) -> dict:
    return _job(ctx.jobs.run_sbx(["versions", "--write"], "Save the versions that the projects need", "versions"))


# --- projects -----------------------------------------------------------------

def projects(ctx: Context, req) -> dict:
    entries = projects_mod.load()
    boxes: dict[str, list[str]] = {}
    error = None
    try:
        for box in ctx.pve().sandboxes():
            boxes.setdefault(box.project, []).append(box.hostname[4:])
    except Exception as exc:  # noqa: BLE001 - the list must work without the host
        error = str(exc)
    out = []
    for name in sorted(entries):
        e = entries[name]
        root = Path(e.checkout) if e.checkout else None
        out.append({"name": name, "checkout": e.checkout, "checkout_exists": bool(root and root.is_dir()),
                    "url": e.url, "token": _keychain_has(ctx, gittoken.keychain_service(name)),
                    "manifest": bool(root and (root / MANIFEST_PATH).is_file()),
                    "sandboxes": boxes.get(projects_mod.tag(name)[9:], [])})
    return {"projects": out, "error": error}


def _git(ctx: Context, root: Path, *args: str) -> str:
    done = ctx.runner.run(["git", "-C", str(root), *args], check=False)
    return done.stdout.strip() if done.code == 0 else ""


def project(ctx: Context, req, name: str) -> dict:
    entries = projects_mod.load()
    if name not in entries:
        raise HttpError(404, f"no project named {name!r}")
    e = entries[name]
    cfg = ctx.cfg()
    root = Path(e.checkout) if e.checkout and Path(e.checkout).is_dir() else None
    out: dict = {"name": name, "checkout": e.checkout, "checkout_exists": root is not None, "url": e.url,
                 "errors": {}}
    manifest = None
    if root is not None:
        try:
            manifest = cli._read_manifest(root)
        except ManifestError as exc:
            out["errors"]["manifest"] = str(exc)
        out["branch"] = _git(ctx, root, "symbolic-ref", "--short", "-q", "HEAD")
        out["unpushed"] = _git(ctx, root, "rev-list", "--count", "@{u}..HEAD")
        out["layout"] = (root / ".sandbox" / "herdr.toml").is_file() and not (root / ".sandbox").is_symlink()
        if manifest is not None:
            path = root / MANIFEST_PATH
            out["manifest_text"] = path.read_text()
    m = manifest or Manifest()
    out["manifest"] = None if manifest is None else {"setup": m.setup, "env_file": m.env_file, "template": m.template}
    binding_path = state_dir() / "bindings" / f"{name}.toml"
    try:
        bindings = load_bindings(binding_path)
    except InputError as exc:
        out["errors"]["bindings"] = str(exc)
        bindings = None
    inputs = []
    if manifest is not None and bindings is not None:
        for d in preview(m, root, dict(os.environ), bindings.inputs):
            i = d.input
            inputs.append({"name": i.name, "kind": i.kind, "required": i.required, "secret": i.secret,
                           "about": i.about, "dest": i.dest, "url": i.url, "placeholder": i.placeholder is not None,
                           "agent_allowed": i.agent_allowed, "source": d.source, "state": d.state,
                           "action": d.action})
    out["inputs"] = inputs
    out["bindings"] = {"path": str(binding_path), "exists": binding_path.exists(),
                       # The kind of each binding, and a command's or a path's
                       # text. A literal `value` may be a secret: never read.
                       "inputs": [{"name": k, "kind": next(iter(v)),
                                   "detail": "" if "value" in v else (shlex.join(v["command"]) if "command" in v
                                                                      else str(v.get("path", "")))}
                                  for k, v in (bindings.inputs.items() if bindings else [])]}
    access = cli._git_access(cfg, bindings) if bindings is not None else None
    out["git"] = {"source": access.source if access else None, "host": access.host if access else None,
                  "token": _keychain_has(ctx, gittoken.keychain_service(name)),
                  "repos": cli._project_repos(cli.Project(name, e.url, None, root, manifest))}
    try:
        out["sandboxes"] = [_box_dict(cfg, b) for b in ctx.pve(cfg).sandboxes() if b.project == projects_mod.tag(name)[9:]]
    except Exception as exc:  # noqa: BLE001
        out["sandboxes"] = []
        out["errors"]["pve"] = str(exc)
    return out


def project_add(ctx: Context, req) -> dict:
    path = _str(req.body, "path", required=True)
    return _job(ctx.jobs.run_sbx(["project", "add", "--", path], f"Add project {path}", "projects"))


def project_forget(ctx: Context, req, name: str) -> dict:
    if not projects_mod.forget(name):
        raise HttpError(404, f"no project named {name!r}")
    ctx.jobs.record(f"Forgot project {name}", f"sbx project rm {name}", f"project:{name}",
                    ["its bindings file and keychain token are untouched"])
    return {"forgotten": True}


def git_token_set(ctx: Context, req, name: str) -> dict:
    if name not in projects_mod.load():
        raise HttpError(404, f"no project named {name!r}")
    token = _secret(req.body, "token")
    args = ["git-token", "--stdin"]
    if host := _str(req.body, "host", _HOST_RE):
        args.append(f"--host={host}")
    if user := _str(req.body, "username", _USER_RE):
        args.append(f"--username={user}")
    if req.body.get("no_check"):
        args.append("--no-check")
    args += [f"--push={_sandbox_name(n)[4:]}" for n in _names_list(req.body, "push", _BOX_RE)]
    return _job(ctx.jobs.run_sbx(args + ["--", name], f"Store the git token of {name}", f"project:{name}",
                                 stdin=(token + "\n").encode()))


def git_token_push(ctx: Context, req, name: str) -> dict:
    """The stored token, into sandboxes that run already: `sbx git-token --push`."""
    if name not in projects_mod.load():
        raise HttpError(404, f"no project named {name!r}")
    wanted = [_sandbox_name(n)[4:] for n in _names_list(req.body, "sandboxes", _BOX_RE)]
    if not wanted:
        raise bad("name at least one sandbox")
    return _job(ctx.jobs.run_sbx(["git-token", *[f"--push={n}" for n in wanted], "--", name],
                                 f"Send the git token of {name} to {', '.join(wanted)}", f"project:{name}"))


def git_token_remove(ctx: Context, req, name: str) -> dict:
    return _job(ctx.jobs.run_sbx(["git-token", "--remove", "--", name], f"Remove the git token of {name}",
                                 f"project:{name}"))


# --- the Claude token -----------------------------------------------------------

def claude(ctx: Context, req) -> dict:
    return _claude_status(ctx)


def claude_set(ctx: Context, req) -> dict:
    token = _secret(req.body, "token")
    if token.startswith("sk-ant-api"):
        raise bad("this is an API key; sbx wants the subscription token that `claude setup-token` prints")
    return _job(ctx.jobs.run_sbx(["claude-token", "--stdin"], "Store a new Claude token and send it to the sandboxes",
                                 "claude-token", stdin=(token + "\n").encode()))


def claude_push(ctx: Context, req) -> dict:
    wanted = [names.hostname(n)[4:] for n in _names_list(req.body, "sandboxes", _BOX_RE)]
    return _job(ctx.jobs.run_sbx(["claude-token", "--push", *wanted], "Send the Claude token to the sandboxes",
                                 "claude-token"))


def claude_remove(ctx: Context, req) -> dict:
    return _job(ctx.jobs.run_sbx(["claude-token", "--remove"], "Remove the Claude token", "claude-token"))


# --- settings -----------------------------------------------------------------

def _reference_rows(heading: str) -> dict[str, list[str]]:
    """The rows of one settings table in docs/reference.md, by key."""
    text = (REPO_ROOT / "docs" / "reference.md").read_text()
    part = text.split(heading, 1)[-1].split("\n### ", 1)[0]
    out = {}
    for line in part.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        key = cells[0].strip("`")
        out[key] = cells
    return out


def _config_toml() -> tuple[Path, dict, str]:
    path = state_dir() / "config.toml"
    if not path.exists():
        return path, {}, ""
    text = path.read_text()
    try:
        return path, tomllib.loads(text), text
    except tomllib.TOMLDecodeError:
        return path, {}, text


def settings(ctx: Context, req) -> dict:
    shared = {v: k for k, v in _ENV_MAP.items()}
    defaults_env = parse_env_file(DEFAULTS_ENV.read_text())
    local_path = local_conf_path()
    local_env = parse_env_file(local_path.read_text()) if local_path.is_file() else {}
    conf_path, conf, conf_text = _config_toml()
    error = None
    try:
        cfg = ctx.cfg()
    except ConfigError as exc:
        cfg, error = None, str(exc)
    mac_rows = _reference_rows("### Mac settings")
    host_rows = _reference_rows("### Host settings")
    mac = []
    for f in dataclasses.fields(Config):
        default = f.default if f.default is not dataclasses.MISSING else f.default_factory()
        env_key = shared.get(f.name)
        if f.name in conf:
            origin = "config.toml"
        elif env_key and env_key in local_env:
            origin = "local.conf"
        elif env_key and env_key in defaults_env:
            origin = "defaults.conf"
        else:
            origin = "default"
        kind = {"int": "int", "list[str]": "list"}.get(str(f.type), "str")
        row = host_rows.get(env_key) if env_key else mac_rows.get(f.name)
        mac.append({"key": f.name, "type": kind, "value": getattr(cfg, f.name) if cfg else conf.get(f.name),
                    "default": default, "origin": origin, "shared": env_key, "editable": env_key is None,
                    "description": (row[-1] if row else ""), "default_text": (row[-2] if row and not env_key else "")})
    host = []
    for key, value in defaults_env.items():
        row = host_rows.get(key)
        host.append({"key": key, "default": value, "local": local_env.get(key), "mac_key": _ENV_MAP.get(key),
                     "description": row[-1] if row else ""})
    return {"error": error, "mac": mac, "host": host,
            "files": {"config.toml": {"path": str(conf_path), "text": conf_text, "exists": conf_path.exists()},
                      "local.conf": {"path": str(local_path), "text": local_path.read_text() if local_path.is_file() else "",
                                     "exists": local_path.is_file()},
                      "defaults.conf": {"path": str(DEFAULTS_ENV), "text": DEFAULTS_ENV.read_text(), "exists": True}},
            "state_dir": str(state_dir())}


def _validate_and_replace(path: Path, text: str) -> None:
    """Write `text` to `path` only when the whole setup loads with it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".portal-tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    try:
        load_config(config_path=tmp)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        tmp.unlink(missing_ok=True)
        raise bad(f"not saved: {exc}") from None
    os.replace(tmp, path)


def settings_save(ctx: Context, req) -> dict:
    """Set or unset Mac settings in config.toml. A shared key is refused: it
    lives in host/local.conf, which the host scripts read too."""
    from ..hostsetup import set_toml_keys
    values = req.body.get("values")
    if not isinstance(values, dict) or not values:
        raise bad("values must name at least one setting")
    types = {f.name: str(f.type) for f in dataclasses.fields(Config)}
    shared = set(_ENV_MAP.values())
    sets, unsets = {}, []
    for key, value in values.items():
        if key not in types:
            raise bad(f"unknown setting {key!r}")
        if key in shared:
            raise bad(f"{key} is a shared setting of host/local.conf; `sbx setup` changes it")
        if value is None:
            unsets.append(key)
        elif types[key] == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise bad(f"{key} must be a whole number")
            sets[key] = value
        elif types[key] == "list[str]":
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                raise bad(f"{key} must be a list of strings")
            sets[key] = value
        else:
            if not isinstance(value, str) or "\n" in value:
                raise bad(f"{key} must be one line of text")
            sets[key] = value
    path, _, text = _config_toml()
    lines = [l for l in text.splitlines() if not any(re.match(rf"^\s*{k}\s*=", l) for k in unsets)]
    scratch = path.with_name("config.toml.portal-edit")
    scratch.write_text("\n".join(lines) + ("\n" if lines else ""))
    try:
        if sets:
            set_toml_keys(scratch, sets)
        new_text = scratch.read_text()
    finally:
        scratch.unlink(missing_ok=True)
    _validate_and_replace(path, new_text)
    ctx.jobs.record("Changed settings in config.toml", "write ~/.config/sbx/config.toml", "settings",
                    [f"set {k} = {json.dumps(v)}" for k, v in sets.items()] + [f"unset {k}" for k in unsets])
    return {"saved": True}


def settings_raw(ctx: Context, req) -> dict:
    text = req.body.get("text")
    if not isinstance(text, str) or len(text) > 100_000:
        raise bad("text must be the file")
    path, _, _ = _config_toml()
    _validate_and_replace(path, text)
    ctx.jobs.record("Edited config.toml", "write ~/.config/sbx/config.toml", "settings")
    return {"saved": True}


# --- checks -------------------------------------------------------------------

def checks(ctx: Context, req) -> dict:
    groups = []
    try:
        cfg = ctx.cfg()
    except ConfigError as exc:
        return {"groups": [{"title": "Settings", "checks": [{"status": "FAIL", "name": "settings", "detail": str(exc)}]}]}
    started = time.monotonic()
    mac = doctor.mac_checks(cfg, ctx.runner)
    groups.append({"title": "This Mac", "checks": mac})
    if not any(c.status == "FAIL" and c.name == "config.toml" for c in mac):
        groups.append({"title": "Proxmox", "checks": doctor.api_checks(cfg, ctx.runner, ctx._api)})
    if req.query.get("network", "1") != "0":
        groups.append({"title": "Network", "checks": doctor.network_checks(cfg, ctx.runner)})
    for g in groups:
        g["checks"] = [dataclasses.asdict(c) for c in g["checks"]]
    return {"groups": groups, "seconds": round(time.monotonic() - started, 1)}


def isolation(ctx: Context, req) -> dict:
    return _job(ctx.jobs.run_sbx(["doctor", "--isolation"], "Prove the isolation (two probe sandboxes)", "isolation"))


# --- jobs, docs, the guide ------------------------------------------------------

def jobs(ctx: Context, req) -> dict:
    return {"jobs": [j.summary() for j in ctx.jobs.all()]}


def job(ctx: Context, req, job_id: str) -> dict:
    j = ctx.jobs.get(job_id)
    if j is None:
        raise HttpError(404, "no such job")
    try:
        since = int(req.query.get("since", "0"))
    except ValueError:
        since = 0
    return j.detail(since)


def job_cancel(ctx: Context, req, job_id: str) -> dict:
    if not ctx.jobs.cancel(job_id):
        raise bad("the job is not running")
    return {"cancelled": True}


def _doc_paths() -> dict[str, Path]:
    out = {"README": REPO_ROOT / "README.md", "AGENTS": REPO_ROOT / "AGENTS.md"}
    for p in sorted((REPO_ROOT / "docs").glob("*.md")):
        out[p.stem] = p
    return out


def docs(ctx: Context, req) -> dict:
    out = []
    for name, path in _doc_paths().items():
        first = next((l[2:].strip() for l in path.read_text().splitlines() if l.startswith("# ")), name)
        out.append({"name": name, "title": first})
    return {"docs": out}


def doc(ctx: Context, req, name: str) -> dict:
    path = _doc_paths().get(name)
    if path is None:
        raise HttpError(404, f"no document {name!r}")
    return {"name": name, "text": path.read_text()}


def guide(ctx: Context, req) -> dict:
    return {"text": cli.guide_text(ctx.cfg())}


def ping(ctx: Context, req) -> dict:
    return {"ok": True, "pid": os.getpid()}


# --- the routes -----------------------------------------------------------------

_N = r"(?P<name>[a-z0-9-]{1,63})"
ROUTES = [
    ("GET", r"/api/ping", ping),
    ("GET", r"/api/overview", overview),
    ("GET", r"/api/sandboxes", sandboxes),
    ("POST", r"/api/sandboxes", new_sandbox),
    ("GET", rf"/api/sandboxes/{_N}", sandbox),
    ("DELETE", rf"/api/sandboxes/{_N}", destroy),
    ("GET", rf"/api/sandboxes/{_N}/ports", ports),
    ("GET", rf"/api/sandboxes/{_N}/system", system),
    ("GET", rf"/api/sandboxes/{_N}/tasks", sandbox_tasks),
    ("GET", rf"/api/sandboxes/{_N}/logs/(?P<which>[a-z-]+)", log),
    ("POST", rf"/api/sandboxes/{_N}/power", power),
    ("POST", rf"/api/sandboxes/{_N}/snapshots", snapshot_take),
    ("POST", rf"/api/sandboxes/{_N}/snapshots/(?P<label>[A-Za-z0-9_-]+)/rollback", snapshot_rollback),
    ("DELETE", rf"/api/sandboxes/{_N}/snapshots/(?P<label>[A-Za-z0-9_-]+)", snapshot_delete),
    ("POST", rf"/api/sandboxes/{_N}/expiry", expiry),
    ("POST", rf"/api/sandboxes/{_N}/herdr", herdr_add),
    ("POST", rf"/api/sandboxes/{_N}/layout", layout),
    ("POST", rf"/api/sandboxes/{_N}/gpu", gpu),
    ("GET", rf"/api/sandboxes/{_N}/remote-control", rc_status),
    ("POST", rf"/api/sandboxes/{_N}/remote-control", rc_action),
    ("GET", r"/api/logs", log_names),
    ("GET", r"/api/tasks/(?P<node>[A-Za-z0-9-]+)/(?P<upid>UPID:[^/]+)", task_log),
    ("POST", r"/api/gc", gc),
    ("GET", r"/api/gpu", gpu_status),
    ("POST", r"/api/terminal", terminal),
    ("GET", r"/api/templates", templates),
    ("POST", r"/api/templates", template_new),
    ("POST", r"/api/templates/import", template_import),
    ("GET", r"/api/templates/(?P<name>[a-z][a-z0-9-]{0,23})", template),
    ("PUT", r"/api/templates/(?P<name>[a-z][a-z0-9-]{0,23})", template_save),
    ("DELETE", r"/api/templates/(?P<name>[a-z][a-z0-9-]{0,23})", template_delete_definition),
    ("GET", r"/api/templates/(?P<name>[a-z][a-z0-9-]{0,23})/export", template_export),
    ("GET", r"/api/components", components),
    ("GET", r"/api/components/(?P<name>[a-z][a-z0-9-]{0,31})", component),
    ("GET", r"/api/versions", versions),
    ("POST", r"/api/versions", versions_write),
    ("GET", r"/api/projects", projects),
    ("POST", r"/api/projects", project_add),
    ("GET", r"/api/projects/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,99})", project),
    ("DELETE", r"/api/projects/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,99})", project_forget),
    ("POST", r"/api/projects/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,99})/git-token", git_token_set),
    ("DELETE", r"/api/projects/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,99})/git-token", git_token_remove),
    ("POST", r"/api/projects/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,99})/git-token/push", git_token_push),
    ("GET", r"/api/claude-token", claude),
    ("POST", r"/api/claude-token", claude_set),
    ("POST", r"/api/claude-token/push", claude_push),
    ("DELETE", r"/api/claude-token", claude_remove),
    ("GET", r"/api/settings", settings),
    ("PUT", r"/api/settings", settings_save),
    ("PUT", r"/api/settings/raw", settings_raw),
    ("GET", r"/api/checks", checks),
    ("POST", r"/api/checks/isolation", isolation),
    ("GET", r"/api/jobs", jobs),
    ("GET", r"/api/jobs/(?P<job_id>[0-9a-f-]{1,40})", job),
    ("POST", r"/api/jobs/(?P<job_id>[0-9a-f-]{1,40})/cancel", job_cancel),
    ("GET", r"/api/docs", docs),
    ("GET", r"/api/docs/(?P<name>[A-Za-z-]{1,40})", doc),
    ("GET", r"/api/guide", guide),
]
COMPILED = [(m, re.compile(p + r"$"), fn) for m, p, fn in ROUTES]


def route(method: str, path: str):
    """(the handler, its arguments), or an HttpError: 404 for no path, 405
    for a path with no such method."""
    seen = False
    for m, pattern, fn in COMPILED:
        match = pattern.match(path)
        if match:
            seen = True
            if m == method:
                return fn, match.groupdict()
    raise HttpError(405 if seen else 404, "no such route" if not seen else f"{method} is not allowed here")
