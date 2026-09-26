"""Claude Code Remote Control in a sandbox: a `claude remote-control` server
that claude.ai/code and the Claude phone app can drive.

Remote Control needs a FULL-SCOPE login, the kind `claude auth login` makes.
It refuses the long-lived token that `sbx claude-token` sends (that token is
inference-only, on purpose). A copy of the Mac's own login is no option
either: Claude Code replaces the refresh token when it uses it, so two copies
sign each other out. So every sandbox signs in on its own, once: `claude auth
login` in the sandbox prints a URL, the user opens it on the Mac and clicks
Authorize, the page shows a code, and the code goes back to the sandbox. tmux
in the sandbox gives that login a terminal and a way to type the code in.

The server then runs as a systemd user service in the sandbox, in a tmux
session, which is what Claude Code's documentation recommends for a machine
you are not sitting at. It needs no inbound port: the sandbox makes outbound
HTTPS to Anthropic only, which the agent firewall permits.

A full login carries more than inference: its scopes include
org:create_api_key. An agent sandbox with it could make API keys on the
user's organization. So Remote Control is for PERSONAL sandboxes, and an
agent sandbox gets it only through `sbx remote-control <name> --allow-agent`:
one sandbox at a time, by the user's own hand, never from a setting or a
project's recipe, and never from `sbx new`.

In an agent sandbox with the Claude proxy, ANTHROPIC_BASE_URL and
ANTHROPIC_AUTH_TOKEN point Claude Code at the sidecar, and they would take
precedence over the login. The login, its check and the server run without
them (UNSET_ENV), so Remote Control talks to Anthropic with the login itself.
"""
from __future__ import annotations

import re
import shlex
import time

from .run import CommandError
from .vm import Vm, VmError

ENV_FILE = ".config/sbx/remote-control.env"
LOG_FILE = ".local/state/sbx/remote-control.log"
ALLOWED_PROFILE = "personal"


# The variables that would take precedence over the full login: the long-lived
# token (~/.config/sbx/claude.env), and the proxy's base URL and placeholder.
UNSET_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
_ENV_U = " ".join(f"-u {v}" for v in UNSET_ENV)


def check_profile(profile: str, hostname: str, allow_agent: bool = False) -> None:
    if profile != ALLOWED_PROFILE and not allow_agent:
        raise VmError(f"{hostname} is an {profile} sandbox: Remote Control needs a full claude.ai login, "
                      "which can make API keys on your organization. To put one in this sandbox "
                      "anyway: sbx remote-control <name> --allow-agent")
UNIT = "sbx-remote-control"
LOGIN_SESSION = "sbx-login"
SERVER_SESSION = "sbx-rc"
_URL_RE = re.compile(r"https://claude\.com/[^\s\x1b]+")

WRAPPER = f"""#!/bin/sh
# The Claude Code Remote Control server for this sandbox. Started by the
# systemd user unit {UNIT}; the settings come from ~/{ENV_FILE}.
set -e
. "$HOME/{ENV_FILE}"
# The long-lived token from ~/.config/sbx/claude.env, or the Claude proxy's
# base URL and placeholder, would take precedence over the login and make
# Remote Control refuse to start.
unset {" ".join(UNSET_ENV)}
cd "$RC_DIR"
mkdir -p "$HOME/.local/state/sbx"
# `script` keeps the terminal that the status display wants and appends a
# transcript, so the state can be read after the process is gone. It runs
# its command through $SHELL: with zsh that is ~/.zshenv again, and the
# token comes back. /bin/sh reads nothing.
export SHELL=/bin/sh
exec script -q -f -a -c "claude remote-control --name \\"$RC_NAME\\" --permission-mode \\"$RC_MODE\\" --spawn \\"$RC_SPAWN\\"" "$HOME/{LOG_FILE}"
"""

# A user unit, so it runs as dev with dev's login, and survives every SSH
# session because lingering is on for dev. tmux gives the server the terminal
# that its status display expects; the unit follows the tmux server process.
SERVICE = f"""[Unit]
Description=Claude Code Remote Control server for this sandbox
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
Environment=TERM=xterm-256color
# %h is systemd's specifier for the home directory; $HOME is not expanded here.
ExecStart=/usr/bin/tmux new-session -d -s {SERVER_SESSION} -x 200 -y 50 "%h/.local/bin/sbx-remote-control; sleep 5"
ExecStop=/usr/bin/tmux kill-session -t {SERVER_SESSION}
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
"""


def env_file(directory: str, name: str, mode: str, spawn: str = "same-dir") -> bytes:
    q = shlex.quote
    return (f"RC_DIR={q(directory)}\nRC_NAME={q(name)}\nRC_MODE={q(mode)}\nRC_SPAWN={q(spawn)}\n").encode()


def install_files(vm: Vm) -> None:
    """The wrapper and the unit, idempotent. They are small, so they go in
    from the tool and do not wait for a template rebuild."""
    vm.put(WRAPPER.encode(), ".local/bin/sbx-remote-control", mode="0755")
    vm.put(SERVICE.encode(), f".config/systemd/user/{UNIT}.service", mode="0644")
    vm.run("systemctl --user daemon-reload")


def logged_in(vm: Vm) -> bool:
    """A full-scope login, as `claude auth status` reports it. The long-lived
    token also reports loggedIn, so the env var is unset for the check."""
    done = vm.run(f"env {_ENV_U} claude auth status 2>/dev/null", check=False)
    return done.code == 0 and '"loggedIn": true' in done.stdout and '"claude.ai"' in done.stdout


def _capture(vm: Vm, session: str) -> str:
    done = vm.run(f"tmux capture-pane -p -J -t {session} 2>/dev/null", check=False)
    return done.stdout if done.code == 0 else ""


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


def _log_tail(vm: Vm, lines: int = 40) -> str:
    done = vm.run(f"tail -n {lines} {shlex.quote(LOG_FILE)} 2>/dev/null", check=False)
    return _ANSI.sub("", done.stdout) if done.code == 0 else ""


def start_login(vm: Vm, directory: str, timeout: float = 30.0) -> str:
    """Start `claude auth login` in the sandbox and return the URL it printed."""
    q = shlex.quote
    vm.run(f"tmux kill-session -t {LOGIN_SESSION} 2>/dev/null; "
           f"tmux new-session -d -s {LOGIN_SESSION} -x 220 -y 50 "
           f"{q(f'cd {shlex.quote(directory)} && env {_ENV_U} claude auth login; echo SBX_LOGIN_EXIT=$?; sleep 60')}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _capture(vm, LOGIN_SESSION)
        if m := _URL_RE.search(text):
            return m.group(0)
        if "SBX_LOGIN_EXIT" in text:
            raise VmError("claude auth login ended before it printed a URL:\n" + text.strip()[-800:])
        time.sleep(1)
    raise VmError("claude auth login printed no URL within 30 s")


def finish_login(vm: Vm, code: str, timeout: float = 60.0) -> None:
    """Type the code into the waiting login, then prove the login."""
    if not code or any(c.isspace() for c in code):
        raise VmError("the code is empty or has whitespace in it")
    vm.run(f"tmux send-keys -t {LOGIN_SESSION} {shlex.quote(code)} Enter")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _capture(vm, LOGIN_SESSION)
        if "SBX_LOGIN_EXIT=0" in text or "Login successful" in text:
            break
        if "SBX_LOGIN_EXIT=" in text:
            raise VmError("the sign-in failed:\n" + text.strip()[-800:])
        time.sleep(1)
    vm.run(f"tmux kill-session -t {LOGIN_SESSION} 2>/dev/null", check=False)
    if not logged_in(vm):
        raise VmError("claude auth status does not show a claude.ai login after the sign-in")


def enable(vm: Vm, directory: str, name: str, mode: str, spawn: str = "same-dir") -> str:
    """Write the settings, answer the one-time consent, start the service.
    Returns a short status text."""
    install_files(vm)
    vm.put(env_file(directory, name, mode, spawn), ENV_FILE)
    # Two dialogs that a service could never answer: the workspace trust for
    # the directory, and the one-time Remote Control consent.
    seed = ("import json, os, sys\n"
            "p = os.path.expanduser('~/.claude.json')\n"
            "d = json.load(open(p)) if os.path.exists(p) else {}\n"
            "d.setdefault('hasCompletedOnboarding', True)\n"
            "d['hasUsedRemoteControl'] = True\n"
            "d['remoteDialogSeen'] = True\n"
            "pr = d.setdefault('projects', {}).setdefault(sys.argv[1], {})\n"
            "pr['hasTrustDialogAccepted'] = True\n"
            "json.dump(d, open(p, 'w'))\n"
            "os.chmod(p, 0o600)\n")
    vm.run(f"python3 -c {shlex.quote(seed)} {shlex.quote(directory)}")
    vm.run(f"rm -f {shlex.quote(LOG_FILE)}; systemctl --user enable --now {UNIT} >/dev/null 2>&1; "
           f"systemctl --user restart {UNIT}")
    # If the consent prompt shows after all, answer it in the tmux session.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        pane = _capture(vm, SERVER_SESSION)
        if "Enable Remote Control?" in pane:
            vm.run(f"tmux send-keys -t {SERVER_SESSION} y Enter", check=False)
        text = _log_tail(vm)
        if "claude.ai/code" in text:
            return "running"
        if "Error" in text:
            line = next((l for l in text.splitlines() if "Error" in l), "")
            return "failed: " + line.strip()[:200]
        time.sleep(2)
    return "started; no status line yet"


def disable(vm: Vm) -> None:
    vm.run(f"systemctl --user disable --now {UNIT} >/dev/null 2>&1 || true; rm -f {shlex.quote(ENV_FILE)}", check=False)


def status(vm: Vm) -> str:
    try:
        active = vm.run(f"systemctl --user is-active {UNIT}", check=False).stdout.strip() or "unknown"
    except (CommandError, VmError):
        return "unreachable"
    text = _log_tail(vm, 20)
    last = next((l for l in reversed(text.splitlines()) if l.strip()), "")
    return f"{active}; {last.strip()[:120]}" if last else active
