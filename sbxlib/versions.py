"""`sbx versions`: what the registered projects need, so the template caches
exactly that and nothing typed by hand.

The version managers own the versions. rvm reads .ruby-version, nvm reads
.nvmrc or .node-version, and Go downloads the toolchain that go.mod names.
A project therefore always gets its own versions, whatever the template
holds. The template is a cache: a Ruby that is in it costs nothing, one that
is not costs a compile of about eight minutes at recipe time. So the list
in host/local.conf is derived from the projects, and a rebuild bakes the
union of what is in use. Docker images follow the same rule: the ones that
the projects' compose files name are pulled into the template.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import projects

_RUBY_GEMFILE = re.compile(r"""^\s*ruby\s+['"]([^'"]+)['"]""", re.M)
_GO_MOD = re.compile(r"^(?:go|toolchain)\s+(?:go)?(\d+(?:\.\d+)*)\s*$", re.M)
_IMAGE = re.compile(r"^\s*image:\s*['\"]?([^'\"\s#]+)", re.M)
_COMPOSE_GLOBS = ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml")
_SKIP_DIRS = {"node_modules", ".git", "vendor", "tmp", "log", "env", ".venv"}


@dataclass
class Needs:
    ruby: dict[str, set[str]] = field(default_factory=dict)    # version -> project names
    node: dict[str, set[str]] = field(default_factory=dict)
    go: dict[str, set[str]] = field(default_factory=dict)
    images: dict[str, set[str]] = field(default_factory=dict)

    def add(self, kind: str, value: str, project: str) -> None:
        value = value.strip()
        if value:
            getattr(self, kind).setdefault(value, set()).add(project)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _first_line(path: Path) -> str:
    return _read(path).strip().splitlines()[0].strip() if _read(path).strip() else ""


def _walk(root: Path, depth: int = 3):
    """Directories under root, depth-limited, without the noisy ones."""
    yield root
    if depth == 0:
        return
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return
    for child in children:
        if child.name in _SKIP_DIRS or child.name.startswith("."):
            continue
        yield from _walk(child, depth - 1)


def scan_checkout(root: Path, project: str, needs: Needs) -> None:
    """Every version file and compose image in a checkout, including the
    services that live in subdirectories such as services/api."""
    for d in _walk(root):
        rv = _first_line(d / ".ruby-version")
        if rv:
            needs.add("ruby", rv.removeprefix("ruby-"), project)
        elif (d / "Gemfile").is_file():
            if m := _RUBY_GEMFILE.search(_read(d / "Gemfile")):
                needs.add("ruby", m.group(1), project)
        for name in (".nvmrc", ".node-version"):
            nv = _first_line(d / name)
            if nv:
                needs.add("node", nv.removeprefix("v"), project)
                break
        if (d / "go.mod").is_file():
            for m in _GO_MOD.finditer(_read(d / "go.mod")):
                needs.add("go", m.group(1), project)
        for pattern in _COMPOSE_GLOBS:
            for f in d.glob(pattern):
                for m in _IMAGE.finditer(_read(f)):
                    if "${" not in m.group(1):
                        needs.add("images", m.group(1), project)


def scan_registry(registry: dict[str, projects.Entry] | None = None) -> tuple[Needs, list[str]]:
    """Needs of every registered project that has a checkout on this Mac,
    and the names of those that have none."""
    needs, missing = Needs(), []
    for name, entry in sorted((registry if registry is not None else projects.load()).items()):
        root = Path(entry.checkout) if entry.checkout else None
        if root is None or not root.is_dir():
            missing.append(name)
            continue
        scan_checkout(root, name, needs)
    return needs, missing


def _sort_versions(values) -> list[str]:
    def key(v: str):
        parts = re.findall(r"\d+", v)
        return ([int(p) for p in parts] if parts else [10**9], v)
    return sorted(values, key=key)


def table(needs: Needs, have_ruby: set[str], have_node: set[str], have_images: set[str]) -> str:
    rows = [("KIND", "VERSION", "IN TEMPLATE", "PROJECTS")]
    for kind, data, have in (("ruby", needs.ruby, have_ruby), ("node", needs.node, have_node),
                             ("go", needs.go, None), ("image", needs.images, have_images)):
        for value in _sort_versions(data):
            if have is None:
                status = "any: go.mod picks the toolchain"
            else:
                status = "yes" if value in have else "NO"
            rows.append((kind, value, status, ", ".join(sorted(data[value]))))
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in rows)


def conf_lines(needs: Needs) -> dict[str, str]:
    """The host/local.conf values: the union, oldest first, so the first Ruby
    and Node become the template's defaults."""
    return {
        "SBX_RUBY_VERSIONS": " ".join(_sort_versions(needs.ruby)),
        "SBX_NODE_VERSIONS": " ".join(_sort_versions(needs.node)),
        "SBX_DOCKER_IMAGES": " ".join(sorted(needs.images)),
    }


def write_local_conf(path: Path, values: dict[str, str], header: str = "") -> None:
    """Set these keys in host/local.conf and keep every other line as it is."""
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            out.append(f'{key}="{values[key]}"')
            seen.add(key)
        else:
            out.append(line)
    if not lines:
        out.append(header or "# Written by `sbx versions --write`: what the registered projects need.\n"
                             "# Rebuild the template after a change: host/30-template-build.sh --replace")
    for key, value in values.items():
        if key not in seen:
            out.append(f'{key}="{value}"')
    path.write_text("\n".join(out) + "\n")
