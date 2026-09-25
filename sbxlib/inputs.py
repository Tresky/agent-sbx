"""Decide which manifest inputs reach a sandbox, and where each value comes from.

Two sources of truth, with different trust:

  - The MANIFEST (untrusted, in git) says what a recipe wants.
  - The BINDINGS file (~/.config/sbx/bindings/<project>.toml, trusted, never in
    git) and the CLI flags say what the user grants. Only a bindings file may
    name a command or a path outside the checkout.

Policy by profile:
  personal  every input that can be found is sent; the list is printed.
  agent     EVERY file and env input needs an explicit --with or --without, so
            a scripted run has no prompt to answer wrongly. The manifest's
            `secret` flag is display only: it is untrusted, so it cannot lower
            the gate. `agent = false` cannot be overridden at all.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import Input, Manifest

MAX_FILE_BYTES = 256 * 1024 * 1024

SEND, PLACEHOLDER, WITHHELD, SKIP, REPO = "send", "placeholder", "withheld", "skip", "repo"


class InputError(ValueError):
    pass


@dataclass
class Decision:
    input: Input
    action: str            # SEND | PLACEHOLDER | WITHHELD | SKIP | REPO
    source: str            # human-readable: where the value comes from
    state: str             # human-readable: found / missing / ...
    _path: Path | None = None
    _command: list[str] | None = None
    _literal: str | None = None

    def load(self) -> bytes:
        """The value to send. Called only for SEND and PLACEHOLDER."""
        if self.action == PLACEHOLDER:
            return (self.input.placeholder or "").encode()
        if self._literal is not None:
            return self._literal.encode()
        if self._command is not None:
            # argv list, never a shell string: a binding is trusted, but a value
            # with a space in it must not change what runs.
            done = subprocess.run(self._command, capture_output=True, timeout=60)
            if done.returncode != 0:
                raise InputError(f"{self.input.name}: binding command failed: "
                                 f"{done.stderr.decode(errors='replace').strip()}")
            out = done.stdout
            return out.rstrip(b"\n") if self.input.kind == "env" else out
        assert self._path is not None
        return self._path.read_bytes()


def resolve_checkout_file(root: Path, rel: str) -> Path | None:
    """`rel` inside the checkout `root`, or None when it does not exist.

    Refuses a symlink ANYWHERE on the path. A branch can commit
    `config/master.key -> ~/.ssh/id_ed25519`; after a `git pull` on the Mac a
    plain open() would follow it and send the user's key to the sandbox.
    """
    root = root.resolve()
    current = root
    for part in Path(rel).parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(mode):
            raise InputError(f"{rel}: {current.relative_to(root)} is a symlink; refusing to follow it")
    if not stat.S_ISREG(os.lstat(current).st_mode):
        raise InputError(f"{rel}: not a regular file")
    if not current.resolve().is_relative_to(root):
        raise InputError(f"{rel}: resolves outside the checkout")
    if current.stat().st_size > MAX_FILE_BYTES:
        raise InputError(f"{rel}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB")
    return current


@dataclass
class GitAccess:
    """How an AGENT sandbox of one project reaches its git host. A personal
    sandbox uses the forwarded SSH agent and never reads this."""
    token_command: list[str] = field(default_factory=list)  # argv; prints the token
    host: str = "github.com"
    username: str = "x-access-token"  # GitHub ignores it for a token; GitLab takes any name
    source: str = ""                   # for the `sbx inputs` report


@dataclass
class Bindings:
    inputs: dict[str, dict] = field(default_factory=dict)
    git: GitAccess | None = None       # None: fall back to config.toml


def _argv(value, what: str) -> list[str]:
    if not (isinstance(value, list) and value and all(isinstance(a, str) for a in value)):
        raise InputError(f"bindings: {what} must be a list of strings")
    return value


def load_bindings(path: Path) -> Bindings:
    """~/.config/sbx/bindings/<project>.toml, the one file that may name a
    command or a path outside the checkout. It is on the Mac only."""
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        return Bindings()
    unknown = set(raw) - {"inputs", "git"}
    if unknown:
        raise InputError(f"bindings: unknown top-level key(s) {sorted(unknown)} in {path}")

    table = raw.get("inputs", {})
    for name, entry in table.items():
        keys = set(entry)
        if len(keys & {"command", "path", "value"}) != 1 or keys - {"command", "path", "value"}:
            raise InputError(f"bindings: [inputs.{name}] needs exactly one of command, path, value")
        if "command" in entry:
            _argv(entry["command"], f"[inputs.{name}] command")

    git = None
    if "git" in raw:
        entry = raw["git"]
        if not isinstance(entry, dict) or set(entry) - {"token_command", "host", "username"}:
            raise InputError("bindings: [git] takes only token_command, host, username")
        if "token_command" not in entry:
            raise InputError("bindings: [git] needs token_command")
        git = GitAccess(token_command=_argv(entry["token_command"], "[git] token_command"),
                        host=str(entry.get("host", "github.com")),
                        username=str(entry.get("username", "x-access-token")),
                        source=f"{path.name} [git]")
    return Bindings(inputs=table, git=git)


def _find_source(item: Input, checkout: Path | None, env: dict, bindings: dict) -> Decision | None:
    """A Decision with action SEND when a value is available, else None."""
    bound = bindings.get(item.name)
    if bound:
        if "command" in bound:
            return Decision(item, SEND, f"binding: {bound['command'][0]} ...", "bound", _command=bound["command"])
        if "value" in bound:
            return Decision(item, SEND, "binding: value", "bound", _literal=str(bound["value"]))
        path = Path(bound["path"]).expanduser()
        if not path.is_file():
            raise InputError(f"{item.name}: binding path {path} does not exist")
        return Decision(item, SEND, f"binding: {bound['path']}", "found", _path=path)

    if item.kind == "env":
        if item.name in env:
            return Decision(item, SEND, f"${item.name} on this Mac", "found", _literal=env[item.name])
        return None

    if checkout is None:
        return None
    path = resolve_checkout_file(checkout, item.dest or "")
    if path is None:
        return None
    return Decision(item, SEND, f"./{item.dest}", f"found ({path.stat().st_size} bytes)", _path=path)


def plan(manifest: Manifest, profile: str, checkout: Path | None, env: dict, bindings: dict,
         with_names: set[str], without_names: set[str]) -> list[Decision]:
    """One Decision per input. Raises InputError with EVERY problem at once, so
    the user fixes the command line in one pass and no VM is made before that."""
    names = {i.name for i in manifest.inputs}
    problems = [f"--with/--without names no input: {n}" for n in sorted((with_names | without_names) - names)]
    problems += [f"{n}: both --with and --without" for n in sorted(with_names & without_names)]

    decisions: list[Decision] = []
    for item in manifest.inputs:
        if item.kind == "repo":
            decisions.append(Decision(item, REPO, item.url or "", "git access"))
            continue

        def fallback(reason: str) -> Decision:
            if item.placeholder is not None:
                return Decision(item, PLACEHOLDER, "placeholder", reason)
            return Decision(item, WITHHELD if reason == "withheld" else SKIP, "-", reason)

        if item.name in without_names:
            decisions.append(fallback("withheld"))
            continue

        try:
            found = _find_source(item, checkout, env, bindings)
        except InputError as exc:
            problems.append(str(exc))
            continue

        if profile == "agent" and not item.agent_allowed:
            if item.name in with_names:
                problems.append(f"{item.name}: the manifest sets agent = false; it cannot go to an agent sandbox")
                continue
            decisions.append(fallback("withheld"))
            continue
        # The gate does NOT read item.secret: that flag comes from the manifest,
        # and an agent could clear it on its branch to get a file sent silently.
        if profile == "agent" and item.name not in with_names:
            problems.append(f"{item.name}: pass --with {item.name} or --without {item.name}")
            continue

        if found is not None:
            decisions.append(found)
        elif item.required and item.placeholder is None:
            where = "no checkout given (--from)" if checkout is None and item.kind == "file" else "not found"
            problems.append(f"{item.name}: required, but {where}; add a binding or pass --without {item.name}")
        else:
            decisions.append(fallback("missing" if item.required else "missing, optional"))

    if problems:
        raise InputError("\n".join(problems))
    return decisions


def table(decisions: list[Decision]) -> str:
    rows = [("INPUT", "KIND", "REQUIRED", "SECRET", "SOURCE", "STATE", "ACTION")]
    for d in decisions:
        i = d.input
        rows.append((i.name, i.kind, "yes" if i.required else "no",
                     "-" if i.kind == "repo" else ("yes" if i.secret else "no"),
                     d.source, d.state, d.action))
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in rows)


def preview(manifest: Manifest, checkout: Path | None, env: dict, bindings: dict) -> list[Decision]:
    """What `sbx inputs` shows: availability only, no profile policy applied."""
    out = []
    for item in manifest.inputs:
        if item.kind == "repo":
            out.append(Decision(item, REPO, item.url or "", "git access"))
            continue
        try:
            found = _find_source(item, checkout, env, bindings)
        except InputError as exc:
            out.append(Decision(item, SKIP, "-", f"REFUSED: {exc}"))
            continue
        if found:
            out.append(found)
        elif item.placeholder is not None:
            out.append(Decision(item, PLACEHOLDER, "placeholder", "missing"))
        else:
            out.append(Decision(item, SKIP, "-", "MISSING" if item.required else "missing, optional"))
    return out
