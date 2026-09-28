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
_TOP_KEYS = {"description", "components", "apt", "cores", "memory_mb", "disk_gb", "image_url", "bare"}
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
    # A bare template skips the core (Docker, Node, Chrome, Claude Code, herdr,
    # the mirror): the base packages, the user and the components only. The
    # sidecar template is bare.
    bare: bool = False
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
    # A bare template (a sidecar) runs a few small services: its floor is
    # lower. 3 GB is the size of the smallest cloud image; a disk cannot shrink.
    bare = data.get("bare", False) is True
    for key, low, default in (("cores", 1, 8), ("memory_mb", 256 if bare else 1024, 8192),
                              ("disk_gb", 3 if bare else 20, 60)):
        v = data.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or v < low:
            raise TemplateError(f"{where}: {key} must be a whole number of at least {low}")
        sizes[key] = v
    for key in ("description", "image_url"):
        if not isinstance(data.get(key, ""), str):
            raise TemplateError(f"{where}: {key} must be a string")
    if not isinstance(data.get("bare", False), bool):
        raise TemplateError(f"{where}: bare must be true or false")
    return Definition(name, path, data.get("description", ""), tuple(comps), tuple(apt),
                      image_url=data.get("image_url", ""), bare=data.get("bare", False),
                      settings=settings, **sizes)


def definition_paths(root: Path | None = None) -> dict[str, Path]:
    """Each definition's file, local over shared, without parsing it."""
    root = root or REPO_ROOT
    found: dict[str, Path] = {}
    for d in definition_dirs(root):
        for p in sorted(d.glob("*.toml")) if d.is_dir() else []:
            if p.name != "versions.toml":
                found[p.stem] = p
    return dict(sorted(found.items()))


def load_all(root: Path | None = None) -> dict[str, Definition]:
    root = root or REPO_ROOT
    return {name: parse(p.read_text(), name, p, root) for name, p in definition_paths(root).items()}


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
        "SBX_TEMPLATE_BARE": "1" if defn.bare else "0",
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
    if "sidecar" in defn.components and (root / "sidecar").is_dir():
        # The sidecar component installs these; a change to them is a change
        # to the template.
        files += sorted(p for p in (root / "sidecar").iterdir() if p.is_file() and p.suffix != ".md")
    for p in files:
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:12]


# --- sharing: one file per template -------------------------------------------
#
# `sbx template export` writes a template as one TOML file: its definition, the
# full text of each LOCAL component that it uses, and the name and hash of each
# shared component (the other person has those from git). `sbx template import`
# reads it back into templates/local/ and template/components/local/.

BUNDLE_FORMAT = 1
BUNDLE_MAX_BYTES = 1_000_000
_COMPONENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


@dataclass
class Bundle:
    name: str
    definition: str
    components: dict[str, str] = field(default_factory=dict)   # local: name -> script
    shared: dict[str, str] = field(default_factory=dict)       # shared: name -> sha256


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _toml_string(text: str) -> str:
    """A TOML string that reads back as exactly `text`. A multi-line literal
    string keeps a script readable; a text that contains ''' cannot be one."""
    if "'''" not in text and "\r" not in text:
        return "'''\n" + text + "'''"
    return json.dumps(text, ensure_ascii=False)


def export_bundle(name: str, root: Path | None = None) -> str:
    root = root or REPO_ROOT
    defn = load(name, root)
    lines = [f"# An sbx template: {name}. Import it with: sbx template import <this file>",
             "# It holds a template definition and the scripts of its own components. A",
             "# component runs as root in the template build: read it before you import.",
             "",
             "[sbx_template]",
             f"format = {BUNDLE_FORMAT}",
             f"name = {json.dumps(name)}",
             f"definition = {_toml_string(defn.path.read_text())}"]
    shared = []
    for comp in defn.components:
        path = component_path(comp, root)
        if path.parent.name == "local":
            lines += ["", f"[sbx_template.components.{comp}]", f"script = {_toml_string(path.read_text())}"]
        else:
            shared.append((comp, _sha256(path.read_text())))
    for comp, digest in shared:
        lines += ["", f"[sbx_template.shared.{comp}]", f"sha256 = {json.dumps(digest)}"]
    return "\n".join(lines) + "\n"


def read_bundle(text: str) -> Bundle:
    if len(text.encode()) > BUNDLE_MAX_BYTES:
        raise TemplateError("the template file is larger than 1 MB; it is not a template file")
    try:
        data = tomllib.loads(text).get("sbx_template")
    except tomllib.TOMLDecodeError as exc:
        raise TemplateError(f"the template file is not valid TOML: {exc}") from None
    if not isinstance(data, dict):
        raise TemplateError("this is not an sbx template file: it has no [sbx_template] table")
    if data.get("format") != BUNDLE_FORMAT:
        raise TemplateError(f"template file format {data.get('format')!r} is not known; update sbx")
    name, definition = data.get("name"), data.get("definition")
    if not (isinstance(name, str) and NAME_RE.match(name)):
        raise TemplateError(f"the template file names no valid template ({name!r})")
    if not isinstance(definition, str):
        raise TemplateError("the template file has no definition")
    bundle = Bundle(name, definition)
    for key, attr, field_name in (("components", "components", "script"), ("shared", "shared", "sha256")):
        table = data.get(key, {})
        if not isinstance(table, dict):
            raise TemplateError(f"[sbx_template.{key}] must be a table")
        for comp, entry in table.items():
            if not _COMPONENT_RE.match(comp):
                raise TemplateError(f"'{comp}' is not a component name")
            if not (isinstance(entry, dict) and isinstance(entry.get(field_name), str)):
                raise TemplateError(f"[sbx_template.{key}.{comp}] needs {field_name}")
            getattr(bundle, attr)[comp] = entry[field_name]
    return bundle


@dataclass
class ImportPlan:
    name: str
    writes: dict[Path, str]               # the files to write
    same: list[str]                       # bundled components that are here already, unchanged
    problems: list[str]                   # any one of these stops the import
    warnings: list[str]


def plan_import(bundle: Bundle, as_name: str = "", force: bool = False,
                root: Path | None = None) -> ImportPlan:
    root = root or REPO_ROOT
    name = as_name or bundle.name
    problems, warnings, writes, same = [], [], {}, []
    if not NAME_RE.match(name):
        problems.append(f"'{name}' is not a template name (lowercase letters, digits, hyphens)")
    _, local = definition_dirs(root)
    dest = local / f"{name}.toml"
    # By path: a broken definition elsewhere in this setup must not stop an import.
    paths = definition_paths(root)
    if name in paths:
        mine = paths[name].parent.name == "local"
        if paths[name].read_text() == bundle.definition:
            same.append(f"definition {name}")
        elif force:
            warnings.append(f"replaces your definition {name} ({paths[name].relative_to(root)})")
            writes[dest] = bundle.definition
        else:
            where = "yours" if mine else "a shared one"
            problems.append(f"a template named {name} exists ({where}); pass --as <new-name>, or --force"
                            + (" to replace yours" if mine else " to hide it with this one"))
    else:
        writes[dest] = bundle.definition

    comp_dir = root / "template" / "components" / "local"
    for comp, script in bundle.components.items():
        have = component_path(comp, root)
        if have is None:
            writes[comp_dir / f"{comp}.sh"] = script
        elif have.read_text() == script:
            same.append(f"component {comp}")
        elif force:
            users = sorted(n for n, p in paths.items() if n != name and comp in _components_of(p))
            warnings.append(f"replaces your component {comp}" + (f", which {', '.join(users)} also use"
                                                                   if users else ""))
            writes[comp_dir / f"{comp}.sh"] = script
        else:
            problems.append(f"a different component {comp} exists here ({have.relative_to(root)}); "
                            "pass --force to replace it with the imported one")
    for comp, digest in bundle.shared.items():
        have = component_path(comp, root)
        if have is None:
            problems.append(f"the template needs the shared component {comp}, which this copy of sbx "
                            "does not have; pull the latest sbx")
        elif _sha256(have.read_text()) != digest:
            warnings.append(f"your copy of the shared component {comp} differs from the exporter's; "
                            "the template can build differently. Pull the latest sbx, or ask which is newer")
    if not problems:
        # The definition must load with the components that it will have.
        for comp in bundle.components:
            if comp_dir / f"{comp}.sh" not in writes and component_path(comp, root) is None:
                problems.append(f"component {comp} is missing")
        try:
            _check_with(bundle.definition, name, dest, writes, root)
        except TemplateError as exc:
            problems.append(str(exc))
    return ImportPlan(name, writes, same, problems, warnings)


def _components_of(path: Path) -> list[str]:
    """The component list of a definition file, or none when it does not parse."""
    try:
        comps = tomllib.loads(path.read_text()).get("components", [])
    except (tomllib.TOMLDecodeError, OSError):
        return []
    return comps if isinstance(comps, list) else []


def _check_with(definition: str, name: str, dest: Path, writes: dict[Path, str], root: Path) -> None:
    """Parse the definition as if the planned components were in place,
    without writing them yet."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp)
        for d in ("components", "components/local"):
            (shadow / "template" / d).mkdir(parents=True)
        for src in (root / "template" / "components", root / "template" / "components" / "local"):
            for p in src.glob("*.sh") if src.is_dir() else []:
                sub = "components/local" if src.name == "local" else "components"
                (shadow / "template" / sub / p.name).write_text(p.read_text())
        for path, text in writes.items():
            if path.suffix == ".sh":
                (shadow / "template" / "components" / "local" / path.name).write_text(text)
        parse(definition, name, dest, shadow)


def apply_import(plan: ImportPlan) -> list[Path]:
    """Write the planned files; on any failure, remove what was written."""
    if plan.problems:
        raise TemplateError("; ".join(plan.problems))
    written = []
    try:
        for path, text in plan.writes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            written.append(path)
    except OSError:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return written


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
