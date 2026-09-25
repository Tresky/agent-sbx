"""The project registry: ~/.config/sbx/projects.toml.

It fills itself. Every command that resolves a project from a checkout records
the name, the checkout and the origin, so `sbx projects` lists what this Mac
has used and `--project <name>` works by name afterwards. Nothing in it is a
secret, and nothing in it is needed: a path or a URL always works too.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import state_dir

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class ProjectError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    checkout: str   # "" when the project was only ever named by URL
    url: str        # "" when the checkout has no origin


def registry_path() -> Path:
    return state_dir() / "projects.toml"


def load(path: Path | None = None) -> dict[str, Entry]:
    path = path or registry_path()
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ProjectError(f"{path}: not valid TOML: {exc}") from exc
    out = {}
    for name, entry in raw.get("projects", {}).items():
        if not isinstance(entry, dict) or not _NAME_RE.match(name):
            raise ProjectError(f"{path}: bad entry {name!r}")
        out[name] = Entry(name, str(entry.get("checkout", "")), str(entry.get("url", "")))
    return out


def save(entries: dict[str, Entry], path: Path | None = None) -> None:
    path = path or registry_path()
    lines = ["# Projects this Mac has used with sbx. `sbx projects` shows them;",
             "# `sbx project rm <name>` forgets one. Written by the tool.", ""]
    for name in sorted(entries):
        e = entries[name]
        lines.append(f"[projects.{json.dumps(name)}]")
        if e.checkout:
            lines.append(f"checkout = {json.dumps(e.checkout)}")
        if e.url:
            lines.append(f"url = {json.dumps(e.url)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


def record(name: str, checkout: Path | None, url: str, path: Path | None = None) -> None:
    """Add or refresh one entry. A checkout that is given wins over one that
    was recorded; a URL that is given wins likewise."""
    entries = load(path)
    old = entries.get(name)
    new = Entry(name,
                str(checkout.resolve()) if checkout else (old.checkout if old else ""),
                url or (old.url if old else ""))
    if old != new:
        entries[name] = new
        save(entries, path)


def forget(name: str, path: Path | None = None) -> bool:
    entries = load(path)
    if name not in entries:
        return False
    del entries[name]
    save(entries, path)
    return True


def tag(name: str) -> str:
    """The Proxmox tag that marks a sandbox as made for this project. Tags
    are lower-case and take only [a-z0-9_.+-], so the name is folded."""
    return "sbx-proj-" + re.sub(r"[^a-z0-9_.+-]", "-", name.lower())
