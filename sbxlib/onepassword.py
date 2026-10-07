"""The sandbox SSH key in 1Password: the `op` CLI, and the public keys that an
SSH agent lists.

sbx reads public keys only. The private key is made inside 1Password and stays
there. `op item create` prints the whole item, with the secret fields hidden
unless --reveal is passed; its output is parsed for the two ids and then
dropped: never printed or logged.
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .run import CommandError, Runner

CATEGORY = "SSH Key"
TITLE = "sbx"


class OpError(RuntimeError):
    pass


@dataclass(frozen=True)
class ItemRef:
    vault_id: str
    item_id: str

    @property
    def public_key_uri(self) -> str:
        return f"op://{self.vault_id}/{self.item_id}/public key"


def default_socket() -> str:
    if sys.platform == "darwin":
        return "~/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
    return "~/.1password/agent.sock"


# --- the op CLI ---------------------------------------------------------------

def installed() -> bool:
    return shutil.which("op") is not None


def signed_in(runner: Runner) -> bool:
    return runner.run(["op", "whoami"], check=False).code == 0


def available(runner: Runner) -> bool:
    return installed() and signed_in(runner)


def _run(runner: Runner, argv: list[str]) -> str:
    try:
        return runner.run(argv).stdout
    except CommandError as exc:
        # The stderr of op names the problem; no argv of ours holds a secret.
        raise OpError(f"{' '.join(argv[:3])}: {exc.stderr.strip() or 'exit ' + str(exc.code)}") from None


def _ref(item: dict, what: str) -> ItemRef:
    vault = item.get("vault")
    item_id = item.get("id")
    vault_id = vault.get("id") if isinstance(vault, dict) else None
    if not (isinstance(item_id, str) and isinstance(vault_id, str) and item_id and vault_id):
        raise OpError(f"{what}: no item id in the output of op")
    return ItemRef(vault_id, item_id)


def create_ssh_key(runner: Runner, title: str = TITLE, vault: str = "") -> ItemRef:
    """Make an ed25519 SSH Key item. Without --reveal, op hides the secret
    fields of its JSON; only `id` and `vault.id` are read from it anyway."""
    argv = ["op", "item", "create", "--category", CATEGORY, "--title", title,
            "--ssh-generate-key", "ed25519"]
    if vault:
        argv += ["--vault", vault]
    out = _run(runner, argv + ["--format", "json"])
    try:
        item = json.loads(out)
    except ValueError:
        raise OpError("op item create: the output is not JSON") from None
    if not isinstance(item, dict):
        raise OpError("op item create: the output is not an item")
    return _ref(item, "op item create")


def public_key(runner: Runner, ref: ItemRef) -> str:
    line = _run(runner, ["op", "read", ref.public_key_uri]).strip()
    if key_fields(line) is None:
        raise OpError("op read: the item has no public key")
    return line


def find_ssh_key(runner: Runner, title: str = TITLE) -> ItemRef | None:
    """The SSH Key item with this title. The list holds titles and ids only."""
    out = _run(runner, ["op", "item", "list", "--categories", CATEGORY, "--format", "json"])
    try:
        items = json.loads(out or "[]")
    except ValueError:
        raise OpError("op item list: the output is not JSON") from None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("title") == title:
            return _ref(item, f"op item list ({title})")
    return None


# --- public keys in an SSH agent ---------------------------------------------

def key_fields(line: str) -> tuple[str, str] | None:
    """The type and the base64 of a public key line; the comment is free text."""
    parts = line.split()
    return (parts[0], parts[1]) if len(parts) >= 2 and parts[0].startswith(("ssh-", "ecdsa-", "sk-")) else None


def same_key(a: str, b: str) -> bool:
    fa = key_fields(a)
    return fa is not None and fa == key_fields(b)


def agent_keys(runner: Runner, sock: str | Path) -> list[str]:
    """The public key lines that the agent at `sock` lists. `ssh-add -L` exits
    1 when the agent holds no key, and 2 when it cannot reach the agent."""
    try:
        got = runner.run(["env", f"SSH_AUTH_SOCK={sock}", "ssh-add", "-L"], check=False)
    except CommandError as exc:
        raise OpError(f"ssh-add: {exc.stderr.strip() or 'exit ' + str(exc.code)}") from None
    if got.code == 1:
        return []
    if got.code != 0:
        raise OpError(f"ssh-add -L cannot reach the agent at {sock}")
    return [l for l in got.stdout.splitlines() if key_fields(l)]


def lists_key(lines: list[str], pubkey: str) -> bool:
    return any(same_key(l, pubkey) for l in lines)
