"""Parse and validate .sandbox/sandbox.toml.

The manifest is UNTRUSTED. It lives in the repository, so an agent that works on
a branch can change it, and this tool then reads it on the Mac with access to
the user's files. Every rule here assumes a hostile author:

  - A manifest names DESTINATIONS inside the clone and logical input names. It
    never names a path on the Mac and never names a command.
  - A manifest can make an input stricter (`agent = false`). No key makes an
    input looser; only a CLI flag from the user permits a secret.
  - Unknown keys are errors, so a typo cannot silently drop a restriction.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

KINDS = ("file", "env", "repo")
DEFAULT_SETUP = ".sandbox/setup.sh"
DEFAULT_ENV_FILE = ".sandbox.env"
MANIFEST_PATH = ".sandbox/sandbox.toml"

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_GIT_URL_RES = (
    re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/-]+$"),
    re.compile(r"^ssh://([A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/-]+$"),
    re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~/-]+$"),  # scp form
)

_INPUT_KEYS = {"name", "kind", "dest", "url", "required", "secret", "about", "placeholder", "agent"}
_RECIPE_KEYS = {"setup", "env_file", "template"}
_TEMPLATE_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Input:
    name: str
    kind: str
    required: bool = True
    secret: bool = False
    about: str = ""
    dest: str | None = None         # file: path in the clone; repo: clone target
    url: str | None = None          # repo only
    placeholder: str | None = None  # used when the input is withheld or absent
    agent_allowed: bool = True      # False: never sent to an agent sandbox


@dataclass(frozen=True)
class Manifest:
    setup: str = DEFAULT_SETUP
    env_file: str = DEFAULT_ENV_FILE
    # The template to clone. Safe to take from an untrusted manifest: it can
    # only choose among the templates that the user built.
    template: str = ""
    inputs: tuple[Input, ...] = field(default_factory=tuple)


def safe_relpath(value, what: str, allow_sibling: bool = False) -> str:
    """Return `value` normalised, or raise. The path must stay inside the clone.

    allow_sibling permits exactly `../<one-name>`: a project may build against a
    sibling checkout such as ../ui-kit, so a `repo` input must be able to land there.
    """
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ManifestError(f"{what}: must be a non-empty string of 255 characters or fewer")
    if any(ch in value for ch in "\x00\n\r\\") or value != value.strip():
        raise ManifestError(f"{what}: illegal character in {value!r}")
    if value.startswith(("/", "~", "-")):
        raise ManifestError(f"{what}: {value!r} must be relative to the repository")
    parts = [p for p in PurePosixPath(value).parts if p != "."]
    if not parts:
        raise ManifestError(f"{what}: {value!r} names no file")
    body = parts
    if allow_sibling and parts[0] == "..":
        if len(parts) != 2:
            raise ManifestError(f"{what}: a sibling must be exactly '../<name>', not {value!r}")
        body = parts[1:]
    if ".." in body:
        raise ManifestError(f"{what}: {value!r} leaves the repository")
    if body[0] == ".git":
        raise ManifestError(f"{what}: {value!r} is inside .git")
    return "/".join(parts)


def check_git_url(url, what: str) -> str:
    # A URL that starts with '-' is an option to git, and the ext:: and file://
    # transports run commands or read local paths. Only plain remotes pass.
    if not isinstance(url, str) or not any(r.match(url) for r in _GIT_URL_RES):
        raise ManifestError(f"{what}: {url!r} is not an https, ssh, or user@host:path git URL")
    if ".." in url:
        raise ManifestError(f"{what}: {url!r} contains '..'")
    return url


def _bool(table: dict, key: str, default: bool, what: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ManifestError(f"{what}: '{key}' must be true or false")
    return value


def _parse_input(raw, index: int) -> Input:
    what = f"input #{index + 1}"
    if not isinstance(raw, dict):
        raise ManifestError(f"{what}: must be a table")
    unknown = set(raw) - _INPUT_KEYS
    if unknown:
        raise ManifestError(f"{what}: unknown key(s) {sorted(unknown)}")

    name, kind = raw.get("name"), raw.get("kind")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ManifestError(f"{what}: 'name' must match {_NAME_RE.pattern}")
    what = f"input {name!r}"
    if kind not in KINDS:
        raise ManifestError(f"{what}: 'kind' must be one of {', '.join(KINDS)}")

    about = raw.get("about", "")
    if not isinstance(about, str) or len(about) > 200 or "\n" in about:
        raise ManifestError(f"{what}: 'about' must be one line of 200 characters or fewer")
    placeholder = raw.get("placeholder")
    if placeholder is not None and (not isinstance(placeholder, str) or len(placeholder) > 4096):
        raise ManifestError(f"{what}: 'placeholder' must be a string of 4096 characters or fewer")

    dest = url = None
    if kind == "file":
        dest = safe_relpath(raw.get("dest"), f"{what} dest")
        if "url" in raw:
            raise ManifestError(f"{what}: 'url' is for kind = \"repo\" only")
    elif kind == "env":
        if not _ENV_RE.match(name):
            raise ManifestError(f"{what}: an env name must match {_ENV_RE.pattern}")
        if "dest" in raw or "url" in raw:
            raise ManifestError(f"{what}: an env input takes no 'dest' and no 'url'")
    else:  # repo
        dest = safe_relpath(raw.get("dest"), f"{what} dest", allow_sibling=True)
        url = check_git_url(raw.get("url"), f"{what} url")
        if "placeholder" in raw or "secret" in raw:
            raise ManifestError(f"{what}: a repo input takes no 'placeholder' and no 'secret'")

    return Input(
        name=name, kind=kind, about=about, dest=dest, url=url, placeholder=placeholder,
        required=_bool(raw, "required", True, what),
        secret=_bool(raw, "secret", False, what),
        agent_allowed=_bool(raw, "agent", True, what),
    )


def parse(text: str) -> Manifest:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"not valid TOML: {exc}") from exc

    unknown = set(raw) - {"recipe", "input"}
    if unknown:
        raise ManifestError(f"unknown top-level key(s) {sorted(unknown)}")

    recipe = raw.get("recipe", {})
    if not isinstance(recipe, dict) or set(recipe) - _RECIPE_KEYS:
        raise ManifestError(f"[recipe] takes only {sorted(_RECIPE_KEYS)}")
    setup = safe_relpath(recipe.get("setup", DEFAULT_SETUP), "recipe.setup")
    env_file = safe_relpath(recipe.get("env_file", DEFAULT_ENV_FILE), "recipe.env_file")
    template = recipe.get("template", "")
    if template and not (isinstance(template, str) and _TEMPLATE_RE.match(template)):
        raise ManifestError("recipe.template must be a template name (lowercase letters, digits, hyphens)")

    raw_inputs = raw.get("input", [])
    if not isinstance(raw_inputs, list):
        raise ManifestError("'input' must be an array of tables: [[input]]")
    inputs = tuple(_parse_input(item, i) for i, item in enumerate(raw_inputs))

    seen: set[str] = set()
    dests: set[str] = set()
    for item in inputs:
        if item.name in seen:
            raise ManifestError(f"input name {item.name!r} appears twice")
        seen.add(item.name)
        if item.dest is not None:
            if item.dest in dests or item.dest == env_file:
                raise ManifestError(f"input {item.name!r}: dest {item.dest!r} is used twice")
            dests.add(item.dest)
    return Manifest(setup=setup, env_file=env_file, inputs=inputs, template=template)
