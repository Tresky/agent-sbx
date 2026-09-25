"""Template definitions: what goes into each base template.

A setup can build several templates, one per kind of project. Each template
is a TOML definition that names its COMPONENTS (template/components/*.sh) and
their settings:

    # templates/rails.toml
    description = "Ruby on Rails"
    components  = ["ruby", "rails"]
    [ruby]
    versions = ["3.4.1"]
    [docker]
    images = ["postgres:17"]

Shared definitions and components are in git (templates/, template/components/).
A user's own are not (templates/local/, template/components/local/), and a
local file wins over a shared one of the same name.

This module imports nothing from the rest of sbxlib, so the Proxmox host runs
it as a script during a build (`python3 sbxlib/templates.py env <name>`). The
Mac and the host then read a definition, and compute its fingerprint, the same
way.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_APT_RE = re.compile(r"^[a-z0-9][a-z0-9.+:=~-]*$")
_REQUIRES_RE = re.compile(r"^#\s*requires:\s*(.*)$", re.M)

# Parts of every template that take settings, like a component does.
CORE_TABLES = ("node", "docker")
_TOP_KEYS = {"description", "components", "apt", "cores", "memory_mb", "disk_gb", "image_url"}
# Files that `sbx new` copies into each new sandbox from the checkout. A
# change to them needs no rebuild, so they do not count in the fingerprint.
_REFRESHED = {"zshenv", "zshrc", "sbx_mirror.py"}


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class Definition:
    name: str
    path: Path
    description: str = ""
    components: tuple[str, ...] = ()
    apt: tuple[str, ...] = ()
    cores: int = 8
    memory_mb: int = 8192
    disk_gb: int = 60
    image_url: str = ""
    settings: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def local(self) -> bool:
        return self.path.parent.name == "local"


def definition_dirs(root: Path | None = None) -> tuple[Path, Path]:
    """(shared, local): a local definition wins over a shared one."""
    root = root or REPO_ROOT
    return root / "templates", root / "templates" / "local"


def component_path(name: str, root: Path | None = None) -> Path | None:
    root = root or REPO_ROOT
    for d in (root / "template" / "components" / "local", root / "template" / "components"):
        if (d / f"{name}.sh").is_file():
            return d / f"{name}.sh"
    return None


def components(root: Path | None = None) -> dict[str, Path]:
    """Every component, local over shared."""
    root = root or REPO_ROOT
    out: dict[str, Path] = {}
    for d in (root / "template" / "components", root / "template" / "components" / "local"):
        for p in sorted(d.glob("*.sh")) if d.is_dir() else []:
            out[p.stem] = p
    return dict(sorted(out.items()))


def requires(path: Path) -> list[str]:
    """The components that a component needs before it: `# requires: a b`."""
    return [n for m in _REQUIRES_RE.finditer(path.read_text()) for n in m.group(1).split()]


def describe(path: Path) -> str:
    """The first comment line of a component, for `sbx template components`."""
    for line in path.read_text().splitlines():
        if line.startswith("#") and not line.startswith(("#!", "# requires:")):
            return line.lstrip("# ").strip()
    return ""


def _value(where: str, value) -> object:
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise TemplateError(f"{where}: a value must be a string, a number, true/false, or a list of strings")


def parse(text: str, name: str, path: Path, root: Path | None = None) -> Definition:
    root = root or REPO_ROOT
    where = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    if not NAME_RE.match(name):
        raise TemplateError(f"{where}: '{name}' is not a template name (lowercase letters, digits, hyphens)")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TemplateError(f"{where}: {exc}") from None

    comps = data.get("components", [])
    if not (isinstance(comps, list) and all(isinstance(c, str) for c in comps)):
        raise TemplateError(f"{where}: components must be a list of names")
    seen: list[str] = []
    for comp in comps:
        path_ = component_path(comp, root)
        if path_ is None:
            raise TemplateError(f"{where}: no component '{comp}' in template/components/")
        if comp in seen:
            raise TemplateError(f"{where}: component '{comp}' is listed twice")
        missing = [r for r in requires(path_) if r not in seen]
        if missing:
            raise TemplateError(f"{where}: component '{comp}' needs {', '.join(missing)} before it in the list")
        seen.append(comp)

    settings: dict[str, dict[str, object]] = {}
    for key, value in data.items():
        if key in _TOP_KEYS:
            continue
        if not isinstance(value, dict):
            raise TemplateError(f"{where}: unknown key '{key}'")
        if key not in comps and key not in CORE_TABLES:
            raise TemplateError(f"{where}: [{key}] is not a listed component; list it in components first")
        table = {}
        for k, v in value.items():
            if not _KEY_RE.match(k):
                raise TemplateError(f"{where}: [{key}] {k}: a key is lowercase letters, digits and _")
            table[k] = _value(f"{where}: [{key}] {k}", v)
        settings[key] = table

    apt = data.get("apt", [])
    if not (isinstance(apt, list) and all(isinstance(p, str) and _APT_RE.match(p) for p in apt)):
        raise TemplateError(f"{where}: apt must be a list of package names")
    sizes = {}
    for key, low, default in (("cores", 1, 8), ("memory_mb", 1024, 8192), ("disk_gb", 20, 60)):
        v = data.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or v < low:
            raise TemplateError(f"{where}: {key} must be a whole number of at least {low}")
        sizes[key] = v
    for key in ("description", "image_url"):
        if not isinstance(data.get(key, ""), str):
            raise TemplateError(f"{where}: {key} must be a string")
    return Definition(name, path, data.get("description", ""), tuple(comps), tuple(apt),
                      image_url=data.get("image_url", ""), settings=settings, **sizes)


def load_all(root: Path | None = None) -> dict[str, Definition]:
    root = root or REPO_ROOT
    found: dict[str, Path] = {}
    for d in definition_dirs(root):
        for p in sorted(d.glob("*.toml")) if d.is_dir() else []:
            if p.name != "versions.toml":
                found[p.stem] = p
    return {name: parse(p.read_text(), name, p, root) for name, p in sorted(found.items())}


def load(name: str, root: Path | None = None) -> Definition:
    root = root or REPO_ROOT
    defs = load_all(root)
    if name not in defs:
        known = ", ".join(defs) or "none"
        raise TemplateError(f"no template definition '{name}' (known: {known}); `sbx template new {name}` makes one")
    return defs[name]


# --- the versions that the projects need --------------------------------------

def versions_path(root: Path | None = None) -> Path:
    """What `sbx versions --write` derived from the projects, per template.
    Not in git: it is this setup's."""
    root = root or REPO_ROOT
    return root / "templates" / "local" / "versions.toml"


def derived(name: str, root: Path | None = None) -> dict[str, dict[str, list[str]]]:
    root = root or REPO_ROOT
    path = versions_path(root)
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text()).get(name, {})
    except tomllib.TOMLDecodeError as exc:
        raise TemplateError(f"{path}: {exc}") from None
    return {t: {k: list(v) for k, v in keys.items() if isinstance(v, list)}
            for t, keys in data.items() if isinstance(keys, dict)}


def write_derived(name: str, values: dict[str, dict[str, list[str]]], root: Path | None = None) -> None:
    """Replace one template's section of versions.toml; keep the others."""
    root = root or REPO_ROOT
    path = versions_path(root)
    data = tomllib.loads(path.read_text()) if path.is_file() else {}
    data[name] = values
    lines = ["# Written by `sbx versions --write`: what the projects of each template need.",
             "# A template build adds these to the versions that its definition names."]
    for tpl in sorted(data):
        for table in sorted(data[tpl]):
            lines.append(f"\n[{tpl}.{table}]")
            for key, items in sorted(data[tpl][table].items()):
                lines.append(f"{key} = {json.dumps(list(items))}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def effective_settings(defn: Definition, root: Path | None = None) -> dict[str, dict[str, object]]:
    """The definition's settings, with the derived versions added after its
    own. The definition's first version stays first: it is the default."""
    root = root or REPO_ROOT
    out = {t: dict(v) for t, v in defn.settings.items()}
    for table, keys in derived(defn.name, root).items():
        if table not in defn.components and table not in CORE_TABLES:
            continue
        for key, items in keys.items():
            own = out.setdefault(table, {}).get(key, [])
            own = own if isinstance(own, list) else [str(own)]
            out[table][key] = own + [i for i in items if i not in own]
    return out


# --- what the build reads -------------------------------------------------------

def _env_value(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return " ".join(value)
    return str(value)


def build_env(defn: Definition, root: Path | None = None) -> dict[str, str]:
    """The variables that template/provision.sh and the components read. A
    setting `[ruby] versions` becomes SBX_RUBY_VERSIONS."""
    root = root or REPO_ROOT
    env = {
        "SBX_TEMPLATE_NAME": defn.name,
        "SBX_COMPONENTS": " ".join(defn.components),
        "SBX_APT_PACKAGES": " ".join(defn.apt),
        "SBX_TEMPLATE_CORES": str(defn.cores),
        "SBX_TEMPLATE_MEMORY_MB": str(defn.memory_mb),
        "SBX_TEMPLATE_DISK_GB": str(defn.disk_gb),
    }
    if defn.image_url:
        env["SBX_UBUNTU_IMAGE_URL"] = defn.image_url
    for table, keys in effective_settings(defn, root).items():
        for key, value in keys.items():
            env[f"SBX_{table.upper().replace('-', '_')}_{key.upper()}"] = _env_value(value)
    return dict(sorted(env.items()))


def fingerprint(defn: Definition, root: Path | None = None) -> str:
    """A short hash of everything that goes into the template: the build
    variables, the core scripts and files, and each component. A template whose
    tag carries a different hash is out of date."""
    root = root or REPO_ROOT
    h = hashlib.sha256(json.dumps(build_env(defn, root), sort_keys=True).encode())
    tdir = root / "template"
    files = [tdir / "provision.sh", tdir / "seal.sh"]
    files += sorted(p for p in (tdir / "files").iterdir() if p.is_file() and p.name not in _REFRESHED) \
        if (tdir / "files").is_dir() else []
    files += [component_path(c, root) for c in defn.components]
    for p in files:
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def shell(env: dict[str, str]) -> str:
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items())


def main(argv: list[str]) -> int:
    """The host's entry point: `templates.py env <name>` prints the build
    variables as shell assignments; `templates.py names` prints each name."""
    try:
        if argv[:1] == ["env"] and len(argv) == 2:
            defn = load(argv[1])
            env = build_env(defn)
            env["SBX_TEMPLATE_HASH"] = fingerprint(defn)
            sys.stdout.write(shell(env))
            return 0
        if argv == ["names"]:
            sys.stdout.write("".join(f"{n}\n" for n in load_all()))
            return 0
    except TemplateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("usage: templates.py env <name> | names", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
