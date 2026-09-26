"""Where sbx keeps its secrets: the Proxmox token, the git tokens, the Claude
token.

On macOS, the keychain holds them, through `security`. On another system
there is no keychain, so each secret is a file in SECRETS_DIR, readable by
this user only (the directory 0700, the file 0600). SBX_SECRET_STORE names
the store ("keychain" or "file") instead of the platform; the tests set it.

A secret is named by its service, as in the keychain. The settings name a
command that prints it (`command`), never the value.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import state_dir
from .run import Runner

ACCOUNT = "sbx"


def kind() -> str:
    got = os.environ.get("SBX_SECRET_STORE") or ("keychain" if sys.platform == "darwin" else "file")
    if got not in ("keychain", "file"):
        raise ValueError(f"SBX_SECRET_STORE is {got!r}; it takes keychain or file")
    return got


def secrets_dir() -> Path:
    return state_dir() / "secrets"


def where() -> str:
    """The store, for a message: "the keychain" or the directory."""
    return "the keychain" if kind() == "keychain" else str(secrets_dir()).replace(str(Path.home()), "~")


def _path(service: str) -> Path:
    return secrets_dir() / service


def command(service: str, account: str | None = None) -> list[str]:
    """The argv that prints the secret."""
    if kind() == "keychain":
        return ["security", "find-generic-password", "-s", service] + (["-a", account] if account else []) + ["-w"]
    return ["cat", str(_path(service))]


def get(runner: Runner, service: str, account: str | None = None) -> str:
    """The secret, or "" when the store has none."""
    if kind() == "file":
        try:
            return _path(service).read_text().strip()
        except FileNotFoundError:
            return ""
    done = runner.run(command(service, account), check=False)
    return done.stdout.strip() if done.code == 0 else ""


def exists(runner: Runner, service: str, account: str | None = None) -> bool:
    if kind() == "file":
        return _path(service).is_file()
    argv = ["security", "find-generic-password", "-s", service] + (["-a", account] if account else [])
    return runner.run(argv, check=False).code == 0


def store(runner: Runner, service: str, value: str, account: str = ACCOUNT) -> None:
    if kind() == "keychain":
        runner.run(["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", value])
        return
    d = secrets_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    # The file is 0600 before the value is in it, and replaces the old one whole.
    tmp = _path(service).with_name(f".{service}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    os.replace(tmp, _path(service))


def forget(runner: Runner, service: str, account: str = ACCOUNT) -> bool:
    """Removes the secret. True when there was one."""
    if kind() == "file":
        try:
            _path(service).unlink()
            return True
        except FileNotFoundError:
            return False
    return runner.run(["security", "delete-generic-password", "-s", service, "-a", account], check=False).code == 0
