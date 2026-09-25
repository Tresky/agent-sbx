"""herdr on the Mac keeps "saved machines": one window, a sidebar with Local
and each SSH machine, and the agents and panes of each. A sandbox is an SSH
config alias (`sbx-<name>`, through the block that `sbx setup` wrote), so
`herdr machine add sbx-<name>` is the whole step. herdr starts the server side
itself; the template already has the binary.

Every function here is best effort: herdr is a convenience, and its absence or
failure must not fail a sandbox.
"""
from __future__ import annotations

import json
import shutil

from .run import CommandError, Runner


def available() -> bool:
    return shutil.which("herdr") is not None


def machines(runner: Runner) -> list[dict]:
    if not available():
        return []
    try:
        done = runner.run(["herdr", "machine", "list", "--json"], input=b"", check=False, timeout=20)
    except CommandError:
        return []
    if done.code != 0:
        return []
    try:
        data = json.loads(done.stdout or "[]")
    except ValueError:
        return []
    return [m for m in data if isinstance(m, dict)]


def find(runner: Runner, hostname: str) -> dict | None:
    return next((m for m in machines(runner) if m.get("target") == hostname or m.get("label") == hostname), None)


def add(runner: Runner, hostname: str) -> str:
    """Returns 'added', 'present', or an error text."""
    if not available():
        return "herdr is not installed on this Mac"
    if find(runner, hostname):
        return "present"
    try:
        # stdin closed (input=b""): with a terminal on stdin herdr may ask a
        # question, and with its output in a pipe nobody sees it. It then waits
        # until the timeout. With no terminal it runs its setup on its own.
        # A timeout raises even with check=False; it must not escape.
        done = runner.run(["herdr", "machine", "add", hostname, "--label", hostname],
                          input=b"", check=False, timeout=90)
    except CommandError as exc:
        return "herdr did not answer within 90 s" if exc.code == 124 else str(exc)
    if done.code != 0:
        lines = (done.stderr or done.stdout).strip().splitlines()
        return lines[-1] if lines else f"exit {done.code}"
    return "added"


def remove(runner: Runner, hostname: str) -> bool:
    if not available():
        return False
    machine = find(runner, hostname)
    if machine is None or not machine.get("id"):
        return False
    try:
        return runner.run(["herdr", "machine", "remove", str(machine["id"])], input=b"", check=False, timeout=30).code == 0
    except CommandError:
        return False
