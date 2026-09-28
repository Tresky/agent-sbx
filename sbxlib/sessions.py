"""Claude Code sessions that come back after a reboot (`sbx autostart`).

Claude Code writes ~/.claude/sessions/<pid>.json for each running process:
the session id, its folder, its name, and whether it is on Remote Control. A
file can outlive its process, so a session is LIVE only when /proc/<pid>
exists and its start time (field 22 of /proc/<pid>/stat) equals the file's
procStart; a reused pid has another start time.

Two small programs, installed into the sandbox by the CLI (no template
rebuild):

- sbx-claude-track, every minute from a user timer: writes the live sessions
  to STATE_FILE. A session that the user ends leaves the list on the next
  tick, so only what was running at an unclean stop is resumed. It skips a
  tick while the machine shuts down (/run/nologin), so a shutdown in progress
  never empties the list.
- sbx-claude-resume, once at boot: waits for the network, then starts each
  listed session that is not live again, `claude --resume <id>`, in a tmux
  session of its own server (`tmux -L sbx-resume`). With the full claude.ai
  login it goes back on Remote Control, with the same clean environment as
  the Remote Control server (remotecontrol.UNSET_ENV); without it, it runs
  through the sidecar's proxy as a `claude` in a terminal does.

The resumer's unit stays active after it ran (RemainAfterExit), so systemd
does not kill the tmux server it started; its own socket keeps it out of the
Remote Control server's tmux, whose restart would take it down.
"""
from __future__ import annotations

import json
import shlex

from . import remotecontrol
from .vm import Vm

STATE_FILE = ".local/state/sbx/claude-sessions.json"
RESUME_LOG = ".local/state/sbx/claude-resume.log"
TMUX_SOCKET = "sbx-resume"
TRACK_UNIT = "sbx-claude-track"
RESUME_UNIT = "sbx-claude-resume"

# Shared by both programs: which sessions are live now.
_LIVE = r'''
import glob, json, os

HOME = os.path.expanduser("~")


def start_time(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
    except OSError:
        return None
    # The name in field 2 may hold spaces and parentheses; the fields after
    # the last ")" start at field 3, so field 22 is index 19.
    return data.rsplit(")", 1)[1].split()[19]


def live_sessions(root=None):
    out = {}
    for path in sorted(glob.glob(os.path.join(root or HOME, ".claude/sessions/*.json"))):
        try:
            with open(path) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        sid, pid = d.get("sessionId"), d.get("pid")
        if d.get("kind") != "interactive" or not sid or not pid:
            continue
        if start_time(pid) != str(d.get("procStart")):
            continue
        out[sid] = {"sessionId": sid, "cwd": d.get("cwd") or HOME, "name": d.get("name") or "",
                    "remote": bool(d.get("bridgeSessionId"))}
    return [out[k] for k in sorted(out)]
'''

TRACKER = f'''#!/usr/bin/env python3
"""sbx-claude-track: the live Claude Code sessions, for sbx-claude-resume.
--print: show them, write nothing."""
import sys, tempfile
{_LIVE}
if __name__ == "__main__":
    live = live_sessions()
    if "--print" in sys.argv:
        print(json.dumps(live, indent=1))
        sys.exit(0)
    if os.path.exists("/run/nologin"):
        sys.exit(0)  # shutting down: keep the list from before
    path = os.path.join(HOME, "{STATE_FILE}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as fh:
        json.dump(live, fh, indent=1)
    os.replace(tmp, path)
'''

RESUMER = f'''#!/usr/bin/env python3
"""sbx-claude-resume: start again the sessions that were live before the boot."""
import shlex, subprocess, time, urllib.error, urllib.request
{_LIVE}
UNSET = {list(remotecontrol.UNSET_ENV)!r}
LOG = os.path.join(HOME, "{RESUME_LOG}")


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\\n")


def online(timeout=180):
    # The sidecar and the gateway may still be booting. Any HTTP answer will do.
    if os.environ.get("SBX_RESUME_NO_WAIT"):  # the tests
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen("https://api.anthropic.com", timeout=5)
            return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            time.sleep(5)
    return False


def full_login():
    env = {{k: v for k, v in os.environ.items() if k not in UNSET}}
    done = subprocess.run(["claude", "auth", "status"], env=env, capture_output=True, text=True)
    return done.returncode == 0 and '"loggedIn": true' in done.stdout and '"claude.ai"' in done.stdout


def mode():
    try:
        with open(os.path.join(HOME, "{remotecontrol.ENV_FILE}")) as fh:
            for line in fh:
                if line.startswith("RC_MODE="):
                    return shlex.split(line.split("=", 1)[1])[0]
    except (OSError, ValueError, IndexError):
        pass
    return "acceptEdits"


def command(entry, remote, perm):
    sid = shlex.quote(entry["sessionId"])
    if not remote:
        return f"claude --resume {{sid}}"
    unset = " ".join(f"-u {{v}}" for v in UNSET)
    name = shlex.quote(entry["name"] or entry["sessionId"][:8])
    return (f"env {{unset}} SHELL=/bin/sh claude --resume {{sid}} --remote-control {{name}} "
            f"--permission-mode {{shlex.quote(perm)}}")


if __name__ == "__main__":
    try:
        with open(os.path.join(HOME, "{STATE_FILE}")) as fh:
            wanted = json.load(fh)
    except (OSError, ValueError):
        wanted = []
    if not wanted:
        log("nothing to resume")
        raise SystemExit(0)
    if not online():
        log("no network after 3 minutes; resuming anyway")
    # Every session goes back on Remote Control when the login allows it,
    # also one that was started in a terminal.
    remote = full_login()
    perm = mode()
    live = {{s["sessionId"] for s in live_sessions()}}
    for entry in wanted:
        if entry["sessionId"] in live:
            log(f"{{entry['sessionId']}} is live already")
            continue
        tmux_name = "resume-" + entry["sessionId"][:8]
        cmd = command(entry, remote, perm)
        done = subprocess.run(["tmux", "-L", "{TMUX_SOCKET}", "new-session", "-d", "-s", tmux_name,
                               "-x", "200", "-y", "50", "-c", entry["cwd"], cmd])
        log(f"{{entry['sessionId']}} in {{entry['cwd']}} -> tmux -L {TMUX_SOCKET} {{tmux_name}} "
            f"({{'Remote Control' if remote else 'terminal only'}}): exit {{done.returncode}}")
'''

TRACK_SERVICE = f"""[Unit]
Description=sbx: record the live Claude Code sessions, for a resume after a reboot
# The resume reads the list from before the boot; do not overwrite it first.
After={RESUME_UNIT}.service

[Service]
Type=oneshot
ExecStart=%h/.local/bin/sbx-claude-track
"""

TRACK_TIMER = f"""[Unit]
Description=sbx: record the live Claude Code sessions every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
AccuracySec=10s
Unit={TRACK_UNIT}.service

[Install]
WantedBy=timers.target
"""

RESUME_SERVICE = f"""[Unit]
Description=sbx: resume the Claude Code sessions that were live before the boot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# Stays active after the script: systemd then leaves the tmux server that it
# started alone, and with it the resumed sessions.
RemainAfterExit=yes
TimeoutStartSec=300
Environment=TERM=xterm-256color
ExecStart=%h/.local/bin/sbx-claude-resume

[Install]
WantedBy=default.target
"""


def install(vm: Vm) -> None:
    """The two programs and three units, idempotent. Nothing that runs now is
    touched: the timer starts ticking, and the resumer only runs at a boot."""
    vm.put(TRACKER.encode(), ".local/bin/sbx-claude-track", mode="0755")
    vm.put(RESUMER.encode(), ".local/bin/sbx-claude-resume", mode="0755")
    vm.put(TRACK_SERVICE.encode(), f".config/systemd/user/{TRACK_UNIT}.service", mode="0644")
    vm.put(TRACK_TIMER.encode(), f".config/systemd/user/{TRACK_UNIT}.timer", mode="0644")
    vm.put(RESUME_SERVICE.encode(), f".config/systemd/user/{RESUME_UNIT}.service", mode="0644")
    vm.run("systemctl --user daemon-reload")


def enable(vm: Vm) -> None:
    install(vm)
    # enable WITHOUT --now for the resumer: it must not start a second copy of
    # a session that is live now. The timer takes its first record at once.
    vm.run(f"systemctl --user enable {RESUME_UNIT}.service >/dev/null 2>&1 && "
           f"systemctl --user enable --now {TRACK_UNIT}.timer >/dev/null 2>&1 && "
           f"systemctl --user start {TRACK_UNIT}.service")


def disable(vm: Vm) -> None:
    vm.run(f"systemctl --user disable --now {TRACK_UNIT}.timer >/dev/null 2>&1; "
           f"systemctl --user disable {RESUME_UNIT}.service >/dev/null 2>&1; true")


def status(vm: Vm) -> tuple[list[dict], list[dict], str]:
    """(the recorded list, the live sessions now, the tail of the resume log)."""
    q = shlex.quote
    recorded = vm.run(f"cat {q(STATE_FILE)} 2>/dev/null || echo []", check=False).stdout
    live = vm.run("~/.local/bin/sbx-claude-track --print 2>/dev/null || echo []", check=False).stdout
    logtail = vm.run(f"tail -n 8 {q(RESUME_LOG)} 2>/dev/null", check=False).stdout
    try:
        return json.loads(recorded or "[]"), json.loads(live or "[]"), logtail
    except ValueError:
        return [], [], logtail
