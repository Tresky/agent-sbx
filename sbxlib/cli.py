"""The `sbx` command."""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import posixpath
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import claudetoken
from . import doctor
from . import gittoken
from . import herdr as herdr_mod
from . import hostsetup
from . import inputs as inputs_mod
from . import layout as layout_mod
from . import manifest as manifest_mod
from . import names
from . import projects as projects_mod
from . import remotecontrol
from . import templates as templates_mod
from . import versions as versions_mod
from .config import Config, ConfigError, load as load_config, local_conf_path, state_dir
from .inputs import PLACEHOLDER, REPO, SEND, InputError
from .manifest import MANIFEST_PATH, Manifest, ManifestError
from .pve import HttpApi, Pve, PveError, TemplateVm
from .run import CommandError, Runner
from .vm import Vm, VmError, dns_has, resolves

SSH_INCLUDE = "Include ~/.config/sbx/ssh_config"


def info(msg: str):
    print(f"==> {msg}", flush=True)


def warn(msg: str):
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


# --- project ----------------------------------------------------------------

@dataclass
class Project:
    name: str
    url: str
    branch: str | None
    checkout: Path | None          # where `file` inputs come from by default
    manifest: Manifest | None


def _read_manifest(root: Path) -> Manifest | None:
    path = root / MANIFEST_PATH
    # lstat, not exists(): the manifest is untrusted, and a symlink here would
    # make this tool parse (and quote in an error) any file on the Mac.
    for part in (root / ".sandbox", path):
        if part.is_symlink():
            raise ManifestError(f"{part.relative_to(root)} is a symlink; refusing to read it")
    if not path.is_file():
        return None
    return manifest_mod.parse(path.read_text())


def resolve_project(spec: str, branch: str | None, from_path: str | None, runner: Runner,
                    need_remote: bool = True) -> Project:
    """`spec` is a checkout path, a registered project name, or a git URL, in
    that order. A checkout that resolves is recorded in the registry."""
    local = Path(spec).expanduser()
    if not local.is_dir() and "/" not in spec and ":" not in spec:
        known = projects_mod.load().get(spec)
        if known is None:
            raise projects_mod.ProjectError(f"{spec!r} is not a directory, a git URL, or a project that "
                                            "`sbx projects` lists")
        if known.checkout and Path(known.checkout).is_dir():
            local = Path(known.checkout)
        elif known.url:
            spec = known.url
        else:
            raise projects_mod.ProjectError(f"project {spec!r}: its checkout {known.checkout!r} is gone")
    if local.is_dir():
        git = ["git", "-C", str(local)]
        # `sbx inputs` only reads the manifest, so a checkout with no remote is
        # fine there. `sbx new` needs the remote: the sandbox clones from it.
        got = runner.run(git + ["remote", "get-url", "origin"], check=need_remote)
        url = got.stdout.strip() if got.code == 0 else ""
        if not url:
            name = names.project_name(str(local.resolve()))
            projects_mod.record(name, local, "")
            return Project(name, "", branch, local, _read_manifest(local))
        manifest_mod.check_git_url(url, "origin")
        projects_mod.record(names.project_name(url), local, url)
        if branch is None:
            # symbolic-ref, not rev-parse: it also answers in a repository with
            # no commit yet, and it fails cleanly on a detached HEAD.
            head = runner.run(git + ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
            branch = head.stdout.strip() if head.code == 0 and head.stdout.strip() else None
        ahead = runner.run(git + ["rev-list", "--count", "@{u}..HEAD"], check=False)
        if ahead.code == 0 and ahead.stdout.strip() not in ("", "0"):
            warn(f"{ahead.stdout.strip()} local commit(s) are not pushed; the sandbox clones the PUSHED state")
        return Project(names.project_name(url), url, branch, local, _read_manifest(local))

    url = manifest_mod.check_git_url(spec, "--project")
    tmp = Path(tempfile.mkdtemp(prefix="sbx-manifest-"))
    try:
        # Only .sandbox/ is fetched: a sparse, shallow, blob-filtered clone
        # works against every git host, unlike a provider's contents API.
        cmd = ["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse"]
        cmd += ["--branch", branch] if branch else []
        runner.run(cmd + ["--", url, str(tmp / "r")])
        runner.run(["git", "-C", str(tmp / "r"), "sparse-checkout", "set", ".sandbox"])
        found = _read_manifest(tmp / "r")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    checkout = Path(from_path).expanduser() if from_path else None
    if checkout is not None and not checkout.is_dir():
        raise InputError(f"--from {from_path}: not a directory")
    return Project(names.project_name(url), url, branch, checkout, found)


def _bindings(project: Project) -> inputs_mod.Bindings:
    return inputs_mod.load_bindings(state_dir() / "bindings" / f"{project.name}.toml")


def _git_access(cfg: Config, bindings: inputs_mod.Bindings) -> inputs_mod.GitAccess | None:
    """The project's own token first; config.toml's token as the fallback;
    None when there is neither, which means public repositories only."""
    if bindings.git is not None:
        return bindings.git
    if cfg.git_token_command:
        return inputs_mod.GitAccess(token_command=cfg.git_token_command, host=cfg.git_token_host,
                                    source="config.toml git_token_command")
    return None


def _pve(cfg: Config, runner: Runner, api) -> Pve:
    """`api` is a test seam; a real run builds the HTTPS transport."""
    return Pve(cfg, api or HttpApi(cfg, runner))


# --- commands ---------------------------------------------------------------

def _prepare_mac(cfg: Config, runner: Runner) -> None:
    """The state directory, config.toml, the sandbox key and the SSH block.
    `sbx setup` runs it; it is safe to run again."""
    home = state_dir()
    home.mkdir(parents=True, exist_ok=True)
    (home / "bindings").mkdir(exist_ok=True)
    os.chmod(home, 0o700)

    conf = home / "config.toml"
    if not conf.exists():
        conf.write_text(
            '# Settings for sbx. docs/reference.md explains each value.\n'
            'pve_api = ""             # e.g. "https://192.168.1.10:8006"\n'
            '# Prints the API token as USER@REALM!TOKENID=SECRET. Example for the macOS keychain:\n'
            '# pve_token_command = ["security", "find-generic-password", "-s", "sbx-pve-token", "-w"]\n'
            '# pve_ca_file = "~/.config/sbx/pve-root-ca.crt"\n'
            'default_profile = "agent"\n'
            'cores = 8\nmemory_mb = 6144\nagent_ttl_days = 3\n'
            '# A git token for agent sandboxes of EVERY project. Prefer one token per\n'
            '# project in bindings/<project>.toml, [git]; see docs/projects.md.\n'
            '# git_token_command = ["security", "find-generic-password", "-s", "sbx-git-token", "-w"]\n'
            '# The domain, the bridges, the subnets and the IDs live in host/local.conf,\n'
            '# which the host scripts read too.\n')
        info(f"wrote {conf}")

    key = cfg.ssh_key_path
    if not key.exists():
        # A dedicated key: the wildcard SSH block can then say IdentitiesOnly,
        # and no personal key is ever authorised inside a sandbox.
        runner.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "sbx", "-f", str(key)])
        info(f"made the key pair {key}")

    block = home / "ssh_config"
    block.write_text(
        "# Written by `sbx setup`. One wildcard block serves every sandbox, so no\n"
        "# SSH config changes when a sandbox is made or removed.\n"
        "Host sbx-*\n"
        f"  HostName %h.{cfg.domain}\n"
        f"  User {cfg.vm_user}\n"
        f"  IdentityFile {key}\n"
        "  IdentitiesOnly yes\n"
        f"  UserKnownHostsFile {home / 'known_hosts'}\n"
        # No HostKeyAlias: ssh does not expand %h there, so every sandbox would
        # share one literal alias and the second one would trip the key check.
        # ssh keys the entry by the resolved HostName, the full name, and the
        # CLI uses the same as its alias, so `sbx rm` removes the one entry.
        "  StrictHostKeyChecking accept-new\n"
        "  ForwardAgent no\n")
    info(f"wrote {block}")


def _has_ssh_include() -> bool:
    ssh_config = Path("~/.ssh/config").expanduser()
    return ssh_config.exists() and SSH_INCLUDE in ssh_config.read_text()


def _add_ssh_include() -> None:
    ssh_config = Path("~/.ssh/config").expanduser()
    existing = ssh_config.read_text() if ssh_config.exists() else ""
    if SSH_INCLUDE in existing:
        return
    ssh_config.parent.mkdir(mode=0o700, exist_ok=True)
    # Include must come first: ssh takes the first value it sees for an option.
    ssh_config.write_text(SSH_INCLUDE + "\n\n" + existing)
    os.chmod(ssh_config, 0o600)
    info("added the Include line to the top of ~/.ssh/config")


def cmd_inputs(args, cfg: Config, runner: Runner, api=None) -> int:
    project = resolve_project(args.project, args.branch, args.from_path, runner, need_remote=False)
    bindings = _bindings(project)
    manifest = project.manifest or Manifest()
    if project.manifest is None:
        print(f"{project.name}: no {MANIFEST_PATH}; the recipe declares no inputs")
    else:
        print(f"{project.name}: recipe {manifest.setup}")
    if manifest.inputs:
        print(inputs_mod.table(inputs_mod.preview(manifest, project.checkout, dict(os.environ), bindings.inputs)))
        for item in manifest.inputs:
            if item.about:
                print(f"  {item.name}: {item.about}")
    # Which repositories an agent sandbox's token must cover, and where it comes from.
    access = _git_access(cfg, bindings)
    print(f"git access for an agent sandbox: {access.source if access else 'NONE (public repositories only)'}")
    print("  repositories the token must cover: " + ", ".join(_project_repos(project)))
    if access is None:
        print(f"  set it with: sbx git-token {args.project}")
    return 0


GUIDE = """\
sbx: throwaway Proxmox sandboxes for development

WHAT IT IS
  One command makes a VM on the Proxmox host in about 30 s, from one of your
  templates. Each template has the core (Node, Docker, Chrome, Claude Code,
  herdr) and the components that its definition names (Ruby, Go, Rust, ...).
  The VM gets a name at once, and every port on it is direct, with https on
  the same port:

      https://sbx-<name>.{domain}:<port>

TWO PROFILES
  agent      for an AI agent with full permissions. Internet only: no LAN, no
             tailnet, no other sandbox. Secrets go in only when you name them.
             A "clean" snapshot after setup. Expires after {ttl} days.
  personal   for your own work. Internet and LAN. Your SSH agent for git,
             forwarded for the clones only. No expiry.

A SANDBOX, FROM START TO END
  sbx new lab                          a plain sandbox, {profile} profile
  sbx new lab --profile personal       ... for your own work
  sbx new app --project ~/code/app     clone the project and run its recipe
  sbx new lab --template rust          ... from a named template
  sbx ssh lab                          a shell        (or: ssh sbx-lab)
  sbx herdr lab                        put it in your herdr sidebar (done by `new` too)
  sbx layout app --replace             the project's .sandbox/herdr.toml panes, again
  sbx remote-control lab               Claude Code Remote Control (personal sandboxes only)
  sbx claude-token                     sign Claude Code in every sandbox in to your
                                       Claude subscription; again when the token expires
  sbx list                             every sandbox
  sbx snap lab  /  sbx rollback lab    a snapshot, and back to it
  sbx rm lab                           destroy it; `sbx gc` destroys expired ones

A PROJECT
  sbx projects                         the projects this Mac has used
  sbx inputs <project>                 what its recipe needs, and what is found
  sbx git-token <project>              a git token for its agent sandboxes
  sbx versions [--write]               the versions the projects need; the template caches them
  A project carries .sandbox/setup.sh, the recipe, and .sandbox/sandbox.toml,
  what the recipe needs. <project> is a checkout path, a URL, or a listed name.
  An agent sandbox needs --with <input> or --without <input> for each input.

TEMPLATES
  sbx template list                    the definitions, and what is built
  sbx template new <name> --from rails your own definition, in templates/local/
  sbx template rebuild <name>          build a new version (15-40 min; asks for
                                       the host's root password)

INSIDE A SANDBOX
  User {user}, sudo with no password, the project in ~/code/<project>.
  A dev server on 127.0.0.1:<port> is reachable from your Mac at
  https://sbx-<name>.{domain}:<port>, with no bind option.
  A database published by Docker is reachable on its port the same way.

ON THE PROXMOX HOST
  Three things that are not sandboxes. `sbx setup` made them, and they stay.
  {gw_ctid} {gw_host:<9}  a small container: the DHCP and DNS server for the
                  sandboxes, their route to the internet, the firewall that
                  keeps an agent sandbox off your LAN, and the Tailscale route
                  that lets your Mac reach them. A sandbox's name comes from
                  its DHCP request to this container, and from nothing else.
  sbx-tpl-*       the templates, in the pool {template_pool}: one Ubuntu VM per
                  definition and version. A sandbox is a linked clone of one,
                  which is why `sbx new` takes seconds. They never run.
  {agent_bridge}, {personal_bridge}  two bridges with no physical port: the agent subnet
                  ({agent_net}.0/24) and the personal subnet ({personal_net}.0/24).
                  The bridge a sandbox sits on IS its profile: the firewall
                  keys on it, and nothing inside a sandbox can change it.

WHERE THINGS ARE
  {state}/            config.toml, the sandbox key, bindings/, projects.toml
  {docs}/
      usage.md         daily use: profiles, ports, Claude, snapshots
      projects.md      recipes, manifests, inputs, git tokens, pane layouts
      templates.md     one template per kind of project, and your own
      troubleshooting.md, reference.md, security.md, setup.md, architecture.md
  sbx <command> --help   every option of a command
"""


def cmd_guide(args, cfg: Config, runner: Runner, api=None) -> int:
    from .config import REPO_ROOT
    home = str(Path.home())
    print(GUIDE.format(domain=cfg.domain, ttl=cfg.agent_ttl_days, profile=cfg.default_profile,
                       user=cfg.vm_user, state=str(state_dir()).replace(home, "~"),
                       docs=str(REPO_ROOT / "docs").replace(home, "~"),
                       gw_ctid=cfg.gw_ctid, gw_host=cfg.gw_hostname, template_pool=cfg.template_pool,
                       agent_bridge=cfg.agent_bridge, personal_bridge=cfg.personal_bridge,
                       agent_net=cfg.agent_net, personal_net=cfg.personal_net), end="")
    return 0


def _host_ssh(cfg: Config) -> list[str]:
    """ssh to the host's root shell, with a shared connection so that the
    scp and the ssh of one command ask for the password once."""
    state_dir().mkdir(parents=True, exist_ok=True)
    return ["-o", "ControlMaster=auto", "-o", f"ControlPath={state_dir() / 'host-%C'}",
            "-o", "ControlPersist=15m", *cfg.pve_ssh_options]


def _copy_to_host(cfg: Config, runner: Runner) -> None:
    """The scripts, the template definitions (the local ones too) and the
    definition parser go to /root/sbx on the host, over the shared connection."""
    from .config import REPO_ROOT
    target = cfg.pve_ssh_target
    info(f"copying the scripts and the template definitions to {target}:/root/sbx/")
    runner.run(["ssh", *_host_ssh(cfg), target, "mkdir -p /root/sbx"], capture=False, check=False)
    done = runner.run(["scp", *_host_ssh(cfg), "-q", "-r"]
                      + [str(REPO_ROOT / d) for d in ("host", "gw", "template", "templates", "sbxlib")]
                      + [f"{target}:/root/sbx/"], capture=False, check=False)
    if done.code != 0:
        raise CommandError(["scp"], done.code, "the copy to the host failed")


def _host_build(cfg: Config, runner: Runner, *args: str) -> None:
    done = runner.run(["ssh", *_host_ssh(cfg), "-t", cfg.pve_ssh_target, "SBX_YES=1", "bash",
                       "/root/sbx/host/30-template-build.sh", *args], capture=False, check=False)
    if done.code != 0:
        raise CommandError(["30-template-build.sh", *args], done.code, "the host script printed why")


def _definitions() -> dict[str, templates_mod.Definition]:
    try:
        return templates_mod.load_all()
    except templates_mod.TemplateError as exc:
        raise ConfigError(str(exc)) from None


def _state(defn: templates_mod.Definition | None, versions: list[TemplateVm]) -> str:
    if not versions:
        return "not built"
    if defn is None:
        return "no definition"
    try:
        current = templates_mod.fingerprint(defn) == versions[-1].fingerprint
    except (templates_mod.TemplateError, OSError):
        return "?"
    return "current" if current else "OUT OF DATE"


def _stale_warning(tv: TemplateVm) -> str | None:
    defs = _definitions()
    if tv.name in defs and _state(defs[tv.name], [tv]) == "OUT OF DATE":
        return (f"template {tv.name} is older than its definition; "
                f"`sbx template rebuild {tv.name}` builds a new version")
    return None


def _project_template(cfg: Config, project: "Project | None", built: dict) -> str:
    """The template of a sandbox: the project's manifest, then default_template,
    then the only one that is built."""
    if project is not None and project.manifest is not None and project.manifest.template:
        return project.manifest.template
    if cfg.default_template:
        return cfg.default_template
    if len(built) == 1:
        return next(iter(built))
    return ""


def cmd_template(args, cfg: Config, runner: Runner, api=None) -> int:
    from .config import REPO_ROOT
    action = args.action

    if action == "components":
        for name, path in templates_mod.components().items():
            where = "local " if path.parent.name == "local" else "shared"
            needs = templates_mod.requires(path)
            print(f"  {name:<10} {where}  {templates_mod.describe(path)}"
                  + (f"  (after: {', '.join(needs)})" if needs else ""))
        print("A definition lists them in `components`; template/components/<name>.sh shows each setting.")
        return 0

    if action == "new":
        if not templates_mod.NAME_RE.match(args.name):
            raise InputError(f"'{args.name}' is not a template name (lowercase letters, digits, hyphens)")
        shared, local = templates_mod.definition_dirs()
        dest = local / f"{args.name}.toml"
        if dest.exists():
            raise InputError(f"{dest} exists already")
        if args.from_name:
            src = local / f"{args.from_name}.toml"
            src = src if src.is_file() else shared / f"{args.from_name}.toml"
            if not src.is_file():
                raise InputError(f"no definition '{args.from_name}' to start from; `sbx template list` shows them")
            text = src.read_text()
        else:
            text = ('description = ""\n'
                    '# The components, in order: `sbx template components` lists them.\n'
                    'components  = []\n\n'
                    '# Settings of a component go in a table of its name, for example:\n'
                    '# [ruby]\n# versions = ["3.4.1"]\n\n'
                    '[node]\nversions = ["lts/*"]\n\n'
                    '# apt = ["graphviz"]       more apt packages\n'
                    '# disk_gb = 60             also cores, memory_mb, image_url\n')
        local.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        templates_mod.load(args.name)  # refuses a file that does not parse
        info(f"wrote {dest.relative_to(templates_mod.REPO_ROOT)}; edit it, then: sbx template rebuild {args.name}")
        return 0

    if action == "export":
        text = templates_mod.export_bundle(args.name)
        if args.output:
            Path(args.output).expanduser().write_text(text)
            info(f"wrote {args.output}; share it, and import it with: sbx template import <file>")
        else:
            sys.stdout.write(text)
        return 0

    if action == "import":
        return _import_template(args)

    defs = _definitions()
    if action in ("list", "show"):
        pve = _pve(cfg, runner, api)
        built = pve.templates()
        boxes = pve.sandboxes()
        if action == "show":
            name = args.name
            if name not in defs and name not in built:
                raise InputError(f"no template '{name}'; `sbx template list` shows them")
            defn = defs.get(name)
            if defn is not None:
                print(f"{name}: {defn.description or '-'}")
                print(f"  definition  {defn.path.relative_to(templates_mod.REPO_ROOT)}")
                print(f"  components  core, {', '.join(defn.components) or 'nothing more'}")
                for key, value in templates_mod.build_env(defn).items():
                    if key not in ("SBX_TEMPLATE_NAME", "SBX_COMPONENTS"):
                        print(f"  {key:<24} {value or '-'}")
                print(f"  fingerprint {templates_mod.fingerprint(defn)}")
            for v in built.get(name, []):
                users = [b.hostname for b in boxes if b.template == name]
                print(f"  built       {v.vm_name} (VM {v.vmid}, fingerprint {v.fingerprint})")
            if built.get(name):
                users = [b.hostname for b in boxes if b.template == name]
                print(f"  sandboxes   {', '.join(users) or 'none'}")
            return 0
        rows = [("TEMPLATE", "DEFINITION", "STATE", "BUILT", "SANDBOXES", "DESCRIPTION")]
        for name in sorted(set(defs) | set(built)):
            defn, versions = defs.get(name), built.get(name, [])
            source = "-" if defn is None else ("local" if defn.local else "shared")
            newest = f"{versions[-1].vmid}" + (f" (+{len(versions) - 1} old)" if len(versions) > 1 else "") if versions else "-"
            users = sum(1 for b in boxes if b.template == name)
            rows.append((name + (" *" if name == cfg.default_template else ""), source, _state(defn, versions),
                         newest, str(users), defn.description if defn else ""))
        widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
        for r in rows:
            print("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip())
        print("* = default_template.  `sbx template rebuild <name>` builds one;"
              " `sbx template new <name>` starts your own.")
        return 0

    if action == "rebuild":
        pve = _pve(cfg, runner, api)
        built = pve.templates()
        if args.all:
            names = list(defs)
        elif args.changed:
            names = [n for n in defs if _state(defs[n], built.get(n, [])) != "current"]
            if not names:
                info("every template is current")
                return 0
        else:
            names = args.names
        if not names:
            raise InputError("name a template, or pass --all or --changed; `sbx template list` shows them")
        missing = [n for n in names if n not in defs]
        if missing:
            raise InputError(f"no definition for {', '.join(missing)}; `sbx template new <name>` makes one")
        if not args.no_versions:
            _derive_versions(cfg, pve, defs, names)
        minutes = f"{15 * len(names)} to {40 * len(names)}"
        if not _confirm(f"Build {', '.join(names)}? It takes {minutes} minutes and asks for the host's root "
                        "password once. Sandboxes keep their current template version.", args.yes):
            return 1
        _copy_to_host(cfg, runner)
        for name in names:
            info(f"building template {name}; the host script prints its progress")
            _host_build(cfg, runner, name)
        built = pve.templates()
        for name in names:
            if built.get(name):
                info(f"template {name}: {built[name][-1].vm_name} (VM {built[name][-1].vmid}) is ready")
            else:
                warn(f"template {name} was built, but the token does not see it. Run on the host: "
                     "bash /root/sbx/host/40-api-token.sh --acl-only")
        return 0

    if action == "finish":
        _copy_to_host(cfg, runner)
        _host_build(cfg, runner, "--finish", args.name)
        return 0
    if action == "prune":
        _copy_to_host(cfg, runner)
        _host_build(cfg, runner, "--prune")
        return 0
    if action == "rm":
        if not _confirm(f"Remove every version of template {args.name} that no sandbox uses?", args.yes):
            return 1
        _copy_to_host(cfg, runner)
        _host_build(cfg, runner, "--rm", args.name)
        info(f"the definition stays; remove {args.name}.toml from templates/local/ yourself if you want")
        return 0
    if action == "adopt":
        if not templates_mod.NAME_RE.match(args.name):
            raise InputError(f"'{args.name}' is not a template name")
        _copy_to_host(cfg, runner)
        _host_build(cfg, runner, "--adopt", str(args.vmid), args.name)
        return 0
    raise InputError(f"unknown action {action}")


def _read_source(source: str) -> str:
    """A template file from a path, an https URL, or stdin ("-")."""
    if source == "-":
        return sys.stdin.read(templates_mod.BUNDLE_MAX_BYTES + 1)
    if source.startswith(("https://", "http://")):
        if not source.startswith("https://"):
            raise InputError("only an https:// URL is accepted")
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(source, timeout=30) as resp:
                return resp.read(templates_mod.BUNDLE_MAX_BYTES + 1).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            raise InputError(f"cannot read {source}: {exc}") from None
    path = Path(source).expanduser()
    if not path.is_file():
        raise InputError(f"{source}: no such file")
    return path.read_text(errors="replace")


def _import_template(args) -> int:
    bundle = templates_mod.read_bundle(_read_source(args.source))
    plan = templates_mod.plan_import(bundle, args.as_name or "", args.force)
    root = templates_mod.REPO_ROOT
    print(f"template {plan.name}" + (f" (exported as {bundle.name})" if plan.name != bundle.name else ""))
    for problem in plan.problems:
        print(f"  PROBLEM  {problem}")
    if plan.problems:
        raise InputError("nothing imported")
    for warning in plan.warnings:
        warn(warning)
    for item in plan.same:
        print(f"  unchanged  {item}: this Mac has the same already")
    if not plan.writes:
        info("nothing to import: this Mac has the same template already")
        return 0
    # Show every file in full: a component runs as root in the template build.
    for path, text in plan.writes.items():
        print(f"\n----- {path.relative_to(root)} -----")
        print(text.rstrip("\n"))
    print("-----")
    if bundle.components:
        print("A component runs as root in the template build, and its result is in every sandbox of the "
              "template. Read each script above before you import it.")
    if not _confirm(f"Write these {len(plan.writes)} file(s)?", args.yes):
        return 1
    for path in templates_mod.apply_import(plan):
        info(f"wrote {path.relative_to(root)}")
    print(f"Build it: sbx template rebuild {plan.name}")
    return 0


def _registry_by_template(cfg: Config, built: dict) -> tuple[dict[str, versions_mod.Needs], list[str], list[str]]:
    """The needs of each registered project, grouped by the template that its
    sandboxes use. Returns (needs by template, projects with no checkout here,
    projects with no template to go to)."""
    by_tpl: dict[str, versions_mod.Needs] = {}
    missing, homeless = [], []
    for name, entry in sorted(projects_mod.load().items()):
        root = Path(entry.checkout) if entry.checkout else None
        if root is None or not root.is_dir():
            missing.append(name)
            continue
        try:
            manifest = _read_manifest(root)
        except ManifestError:
            manifest = None
        project = Project(name, entry.url, None, root, manifest)
        tpl = _project_template(cfg, project, built)
        if not tpl:
            homeless.append(name)
            continue
        versions_mod.scan_checkout(root, name, by_tpl.setdefault(tpl, versions_mod.Needs()))
    return by_tpl, missing, homeless


def _derive_versions(cfg: Config, pve: Pve, defs: dict, names: list[str]) -> None:
    by_tpl, missing, homeless = _registry_by_template(cfg, pve.templates())
    for name in names:
        needs = by_tpl.get(name)
        if needs is None:
            continue
        values = versions_mod.derived_values(needs, defs[name])
        templates_mod.write_derived(name, values)
        flat = "; ".join(f"{t} {k}: {' '.join(v) or '-'}" for t, kv in values.items() for k, v in kv.items())
        info(f"template {name} will also cache what its projects need: {flat or 'nothing more'}")
    if missing:
        warn("not scanned (no checkout on this Mac): " + ", ".join(missing))
    if homeless:
        warn("no template for: " + ", ".join(homeless) + ". Set [recipe] template in the project, "
             "or default_template in config.toml")


def cmd_versions(args, cfg: Config, runner: Runner, api=None) -> int:
    defs = _definitions()
    built = _pve(cfg, runner, api).templates() if cfg.pve_api else {}
    by_tpl, missing, homeless = _registry_by_template(cfg, built)
    if not by_tpl:
        print("no registered project with a checkout here; `sbx project add <checkout>` records one")
        return 0
    for name, needs in sorted(by_tpl.items()):
        defn = defs.get(name)
        print(f"template {name}" + ("" if defn else " (no definition on this Mac)"))
        have = templates_mod.effective_settings(defn) if defn else {}
        print(versions_mod.table(needs, set(have.get("ruby", {}).get("versions", [])),
                                 set(have.get("node", {}).get("versions", [])),
                                 set(have.get("docker", {}).get("images", [])),
                                 has_ruby=defn is not None and "ruby" in defn.components))
        if args.write and defn is not None:
            templates_mod.write_derived(name, versions_mod.derived_values(needs, defn))
        print()
    if missing:
        print("not scanned (no checkout on this Mac): " + ", ".join(missing))
    if homeless:
        print("no template for: " + ", ".join(homeless) + " (set [recipe] template, or default_template)")
    if args.write:
        info(f"wrote {templates_mod.versions_path()}; the next `sbx template rebuild` caches these")
    else:
        print("`sbx versions --write` saves these for the next build; `sbx template rebuild` does it too.")
    return 0


def cmd_projects(args, cfg: Config, runner: Runner, api=None) -> int:
    action = getattr(args, "action", "list") or "list"
    if action == "add":
        resolve_project(args.project, None, None, runner, need_remote=False)  # records it
        return cmd_projects(argparse.Namespace(action="list"), cfg, runner, api)
    if action == "rm":
        if projects_mod.forget(args.project):
            info(f"forgot {args.project}; its bindings file and keychain token are untouched")
            return 0
        raise projects_mod.ProjectError(f"no project named {args.project!r}")

    entries = projects_mod.load()
    if not entries:
        print("no projects yet; any command with a checkout path records one, e.g. sbx inputs ~/code/app")
        return 0
    # Sandboxes per project, when the host answers; the list must work without it.
    boxes: dict[str, list[str]] = {}
    try:
        for box in _pve(cfg, runner, api).sandboxes():
            boxes.setdefault(box.project, []).append(box.hostname)
    except (ConfigError, PveError, CommandError):
        boxes = {"?": []}
    rows = [("PROJECT", "TOKEN", "CHECKOUT", "SANDBOXES")]
    for name in sorted(entries):
        e = entries[name]
        token = runner.run(["security", "find-generic-password", "-s", gittoken.keychain_service(name), "-a", "sbx"],
                           check=False).code == 0
        checkout = e.checkout.replace(str(Path.home()), "~") if e.checkout else "-"
        if e.checkout and not Path(e.checkout).is_dir():
            checkout += " (gone)"
        mine = boxes.get(projects_mod.tag(name)[9:], [])
        rows.append((name, "yes" if token else "no", checkout, ", ".join(mine) if mine else ("?" if "?" in boxes else "-")))
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    for r in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip())
    return 0


def _project_repos(project: Project) -> list[str]:
    manifest = project.manifest or Manifest()
    return [u for u in [project.url] + [i.url for i in manifest.inputs if i.kind == "repo"] if u]


def cmd_git_token(args, cfg: Config, runner: Runner, api=None) -> int:
    project = resolve_project(args.project, args.branch, args.from_path, runner, need_remote=False)
    binding = state_dir() / "bindings" / f"{project.name}.toml"

    if args.remove:
        gone = gittoken.forget(runner, project.name)
        dropped = gittoken.remove_binding(binding)
        info(f"{project.name}: keychain item {'removed' if gone else 'was absent'}, "
             f"[git] section {'removed' if dropped else 'was absent'}")
        return 0

    repos = _project_repos(project)
    if not repos:
        raise InputError(f"{project.name}: no origin remote, so there is nothing for a token to cover")
    print(f"project: {project.name}")
    print("the token must cover: " + ", ".join(repos))
    if args.host == "github.com":
        print("make it at github.com > Settings > Developer settings > Fine-grained tokens:")
        print("  Only select repositories (the ones above); Contents: Read, or Read and write to push")

    if args.stdin:
        token = sys.stdin.readline().strip()
    else:
        if not sys.stdin.isatty():
            raise InputError("no terminal to ask for the token; pass --stdin and pipe it in")
        token = getpass.getpass(f"token for {project.name} (hidden): ").strip()
    if not token or any(c.isspace() for c in token):
        raise InputError("the token is empty or has whitespace in it")

    if not args.no_check:
        checks = gittoken.check_token(runner, token, args.host, repos)
        for c in checks:
            print(f"  {c.status:<12} {c.repo}")
        if any(c.status == "bad token" for c in checks):
            raise InputError("the git host does not know this token; nothing stored")
        if any(c.status == "no access" for c in checks):
            raise InputError("the token does not cover every repository above; nothing stored. "
                             "Add the missing ones to its repository access, or pass --no-check")

    service = gittoken.store(runner, project.name, token)
    gittoken.write_binding(binding, project.name, args.host, args.username)
    info(f"stored in the keychain as {service}")
    info(f"wrote [git] in {binding}")
    print(f"an agent sandbox of {project.name} now clones with this token: sbx new <name> --project {args.project}")
    return 0


def cmd_claude_token(args, cfg: Config, runner: Runner, api=None) -> int:
    if args.status:
        stored = bool(claudetoken.get(runner))
        end = claudetoken.expires()
        print(f"Claude token: {'stored in the keychain' if stored else 'NONE'}"
              + (f", expires {end}" if stored and end else ""))
        if stored and (note := claudetoken.expiry_warning()):
            warn(note)
        return 0

    if args.remove:
        if args.sandboxes:
            raise InputError("--remove takes no sandbox names; it acts on every running sandbox")
        gone = claudetoken.forget(runner)
        info(f"keychain item {'removed' if gone else 'was absent'}")
        _each_claude_sandbox(cfg, runner, api, [], f"rm -f {shlex.quote(claudetoken.ENV_FILE)}", "token removed")
        return 0

    if args.push:
        token = claudetoken.get(runner)
        if not token:
            raise InputError("no Claude token in the keychain; run: sbx claude-token")
    else:
        token = _new_claude_token(args, runner)
        claudetoken.store(runner, token)
        info(f"stored in the keychain as {claudetoken.SERVICE}; it expires {claudetoken.expires()}")
    return _each_claude_sandbox(cfg, runner, api, args.sandboxes, None, "token updated", token)


def _new_claude_token(args, runner: Runner) -> str:
    if args.stdin:
        token = sys.stdin.readline().strip()
    else:
        if not sys.stdin.isatty():
            raise InputError("no terminal to ask for the token; pass --stdin and pipe it in")
        if shutil.which("claude") is None:
            raise ConfigError("Claude Code is not installed on this Mac. Run `claude setup-token` on a machine "
                              "that has it, then: sbx claude-token --stdin")
        # Interactive: it opens the browser for the sign-in, then prints the token.
        info("running `claude setup-token`; sign in with your Claude account in the browser")
        runner.run(["claude", "setup-token"], capture=False)
        token = getpass.getpass("paste the token that it printed (hidden): ").strip()
    if not token or any(c.isspace() for c in token):
        raise InputError("the token is empty or has whitespace in it")
    if token.startswith("sk-ant-api"):
        # An API key bills per token, outside the subscription.
        raise InputError("this is an API key; sbx wants the subscription token that `claude setup-token` prints")
    return token


def _each_claude_sandbox(cfg: Config, runner: Runner, api, wanted: list[str], command: str | None,
                         done_text: str, token: str = "") -> int:
    """Write the token (or run `command`) in every running sandbox that has
    the token file, and in each sandbox that `wanted` names. A sandbox with no
    file was made with --no-claude, or before a token existed; it is left out
    unless it is named."""
    boxes = _pve(cfg, runner, api).sandboxes()
    named = {names.hostname(n) for n in wanted}
    if unknown := named - {b.hostname for b in boxes}:
        raise PveError("no sandbox named " + ", ".join(sorted(unknown)))
    q = shlex.quote
    failed, stopped = [], []
    for box in boxes:
        if box.status != "running":
            stopped.append(box.hostname)
            continue
        vm = Vm(cfg, runner, box.hostname)
        try:
            if box.hostname not in named:
                has = vm.run(f"test -f {q(claudetoken.ENV_FILE)} && echo yes || echo no")
                if has.stdout.strip() != "yes":
                    continue
            if command is None:
                _install_claude(vm, token)
            else:
                vm.run(command)
            info(f"{box.hostname}: {done_text}")
        except (CommandError, VmError):
            failed.append(box.hostname)
    if stopped:
        # A stopped sandbox cannot be checked or changed.
        warn("not changed, because they are stopped: " + ", ".join(stopped)
             + ". Start them, then: sbx claude-token --push")
    if failed:
        warn("could not reach: " + ", ".join(failed) + ". Try again: sbx claude-token --push")
        return 1
    return 0


def _rc_mode(cfg: Config, profile: str) -> str:
    return cfg.remote_control_mode if profile == remotecontrol.ALLOWED_PROFILE else ""


def _rc_directory(project: Project | None) -> str:
    # Never the home directory: Claude Code refuses to save trust for it.
    return f"/home/dev/code/{project.name}" if project else "/home/dev/code"


def _remote_control_setup(cfg: Config, runner: Runner, vm: Vm, hostname: str, profile: str,
                          project: Project | None, mode: str) -> bool:
    """Sign the sandbox in (one click and one paste, on the Mac) and start the
    server. Returns whether the server runs."""
    remotecontrol.check_profile(profile, hostname)
    directory = _rc_directory(project)
    if not remotecontrol.logged_in(vm):
        if not sys.stdin.isatty():
            warn(f"Remote Control needs a sign-in, which needs a terminal. Later: sbx remote-control {hostname[4:]}")
            return False
        url = remotecontrol.start_login(vm, directory)
        print("\nRemote Control: sign the sandbox in. A browser opens on this Mac; click Authorize,")
        print(f"copy the code the page shows, and paste it here.\n  {url}")
        if runner.run(["open", url], check=False).code != 0:
            print("  (the browser did not open; use the link above)")
        code = getpass.getpass("code (hidden): ").strip()
        remotecontrol.finish_login(vm, code)
        info(f"{hostname} is signed in to claude.ai")
    name = f"{hostname}" + (f" · {project.name}" if project else "")
    state = remotecontrol.enable(vm, directory, name, mode)
    if state.startswith("failed"):
        warn(f"Remote Control {state}")
        return False
    info(f"Remote Control server is {state}: look for {name!r} at claude.ai/code")
    return True


def cmd_remote_control(args, cfg: Config, runner: Runner, api=None) -> int:
    hostname = names.hostname(args.name)
    box = _pve(cfg, runner, api).require(hostname)
    remotecontrol.check_profile(box.profile, hostname)
    vm = Vm(cfg, runner, hostname)
    if args.off:
        remotecontrol.disable(vm)
        info(f"{hostname}: Remote Control server stopped and disabled; the sign-in stays")
        return 0
    if args.status:
        print(f"{hostname}: {remotecontrol.status(vm)}")
        return 0
    mode = args.mode or _rc_mode(cfg, box.profile) or "acceptEdits"
    project = None
    if box.project:
        known = projects_mod.load().get(box.project)
        project = Project(box.project, known.url if known else "", None, None, None)
    return 0 if _remote_control_setup(cfg, runner, vm, hostname, box.profile, project, mode) else 1


def _mkcert_root(runner: Runner) -> Path | None:
    done = runner.run(["mkcert", "-CAROOT"], check=False)
    root = Path(done.stdout.strip()) / "rootCA.pem" if done.code == 0 and done.stdout.strip() else None
    return root if root and root.exists() else None


def _install_cert(cfg: Config, runner: Runner, vm: Vm) -> bool:
    if shutil.which("mkcert") is None or not _mkcert_root(runner):
        warn("no mkcert CA on this Mac (`sbx doctor` explains); this sandbox serves http:// only")
        return False
    certdir = state_dir() / "certs" / vm.hostname
    certdir.mkdir(parents=True, exist_ok=True)
    os.chmod(certdir, 0o700)
    cert, key = certdir / "cert.pem", certdir / "key.pem"
    # A leaf for THIS sandbox's names only. The CA key stays on the Mac, so a
    # sandbox can never mint a certificate that the Mac trusts.
    runner.run(["mkcert", "-cert-file", str(cert), "-key-file", str(key), cfg.fqdn(vm.hostname), vm.hostname])
    vm.put(cert.read_bytes(), "/etc/sbx/tls/cert.pem", mode="0644", sudo=True)
    vm.put(key.read_bytes(), "/etc/sbx/tls/key.pem", mode="0640", sudo=True, group="caddy")
    vm.run("sudo systemctl restart caddy sbx-mirror")
    return True


def _install_claude(vm: Vm, token: str) -> None:
    """Sign Claude Code in the sandbox in to the subscription. The token goes
    over stdin, as every secret does."""
    vm.put(claudetoken.env_file(token), claudetoken.ENV_FILE)
    vm.run(claudetoken.SKIP_ONBOARDING)


def _identity_hint(runner: Runner, host: str) -> str:
    """The ssh-add line for the key that the user's own config uses for `host`."""
    done = runner.run(["ssh", "-G", host], check=False)
    for line in done.stdout.splitlines():
        if line.lower().startswith("identityfile "):
            return f"ssh-add --apple-use-keychain {line.split(None, 1)[1]}"
    return "ssh-add --apple-use-keychain ~/.ssh/<the key for that host>"


def _preflight_git(cfg: Config, runner: Runner, profile: str, project: Project) -> None:
    """Prove that the sandbox will be able to clone, BEFORE a VM exists.

    A personal sandbox sees only the forwarded SSH agent: not the user's ssh
    config, not the identity files it names. An agent sandbox sees only its
    project's token. Both were dead ends after the VM was made.
    """
    repos = _project_repos(project)
    if not repos:
        return
    hosts = [p[0] for u in repos if (p := gittoken.repo_path(u))]
    host = hosts[0] if hosts else "github.com"
    if profile == "personal":
        if runner.run(["ssh-add", "-l"], check=False).code != 0:
            raise InputError("a personal sandbox clones with your forwarded SSH agent, and the agent holds no key.\n"
                             f"  Add the key first:  {_identity_hint(runner, host)}")
        if "github.com" in hosts:
            # What the sandbox will see: the agent alone, no config, no files.
            probe = runner.run(["ssh", "-F", "/dev/null", "-o", "IdentitiesOnly=no", "-o", "BatchMode=yes",
                                "-o", "ConnectTimeout=8", "-T", "git@github.com"], check=False)
            if "successfully authenticated" not in probe.stdout + probe.stderr:
                raise InputError("GitHub accepts no key in your SSH agent, and the sandbox sees only the agent.\n"
                                 f"  Add the key first:  {_identity_hint(runner, 'github.com')}")
        return
    access = _git_access(cfg, _bindings(project))
    if access is None:
        warn(f"no git token for {project.name}; only a public repository can be cloned. "
             f"For a private one: sbx git-token {project.name}")
        return
    token = runner.run(access.token_command).stdout.strip()
    if not token:
        raise InputError(f"{access.source}: the token command printed nothing")
    bad = [c for c in gittoken.check_token(runner, token, access.host, repos) if c.status in ("no access", "bad token")]
    if bad:
        raise InputError(f"the git token for {project.name} cannot read: " + ", ".join(c.repo for c in bad)
                         + f"\n  Make one that covers every repository, then: sbx git-token {project.name}")


def _git_auth(cfg: Config, runner: Runner, vm: Vm, profile: str, project: Project) -> bool:
    """Returns whether git commands in the VM need the forwarded SSH agent.
    _preflight_git has already proved that the access works."""
    if profile == "personal":
        return True
    access = _git_access(cfg, _bindings(project))
    if access is None:
        return False
    token = runner.run(access.token_command).stdout.strip()
    host = access.host
    vm.put(f"https://{access.username}:{token}@{host}\n".encode(), ".git-credentials")
    q = shlex.quote
    # The manifest and `origin` may use the SSH form; the token works over HTTPS.
    vm.run("git config --global credential.helper store && "
           f"git config --global url.{q(f'https://{host}/')}.insteadOf {q(f'git@{host}:')} && "
           f"git config --global --add url.{q(f'https://{host}/')}.insteadOf {q(f'ssh://git@{host}/')}")
    return False


def _setup_project(vm: Vm, project: Project, decisions, forward_agent: bool, profile: str = "agent") -> int:
    q = shlex.quote
    proj_dir = f"code/{project.name}"
    branch = f"--branch {q(project.branch)} " if project.branch else ""
    info(f"cloning {project.url}" + (f" ({project.branch})" if project.branch else ""))
    vm.run(f"git clone -q {branch}-- {q(project.url)} {q(proj_dir)}", forward_agent=forward_agent, capture=False)

    manifest = project.manifest or Manifest()
    # What the recipe cannot learn from inside: the name a browser on the Mac
    # reaches the sandbox by, and the profile. A recipe builds every URL that
    # the Mac must follow (Rails, an OIDC provider, CORS) from SBX_FQDN.
    env_lines = [f"SBX_HOSTNAME={q(vm.hostname)}", f"SBX_FQDN={q(vm.cfg.fqdn(vm.hostname))}",
                 f"SBX_PROFILE={q(profile)}"]
    excludes = []
    for d in decisions:
        item = d.input
        if d.action == REPO:
            dest = posixpath.normpath(posixpath.join(proj_dir, item.dest))
            info(f"cloning {item.url} -> ~/{dest}")
            vm.run(f"test -d {q(dest)} || git clone -q -- {q(item.url)} {q(dest)}",
                   forward_agent=forward_agent, capture=False)
        elif d.action in (SEND, PLACEHOLDER):
            data = d.load()
            if item.kind == "file":
                vm.put(data, f"{proj_dir}/{item.dest}")
                excludes.append("/" + item.dest)
            else:
                env_lines.append(f"{item.name}={q(data.decode())}")
    vm.put(("\n".join(env_lines) + "\n").encode(), f"{proj_dir}/{manifest.env_file}")
    excludes.append("/" + manifest.env_file)
    if excludes:
        # .git/info/exclude is local to this clone: the sent files cannot be
        # committed by accident, and the repository itself does not change.
        vm.run(f"cat >> {q(proj_dir + '/.git/info/exclude')}", input=("\n".join(excludes) + "\n").encode())

    has_recipe = vm.run(f"test -f {q(proj_dir + '/' + manifest.setup)}", check=False).code == 0
    if not has_recipe:
        if project.manifest is not None:
            raise VmError(f"the manifest names {manifest.setup}, but the clone does not have it")
        info("no recipe in this repository; the clone is all the sandbox gets")
        return 0
    # The runner from THIS checkout, not the template's copy: a fix to it then
    # reaches a sandbox at once, and does not wait for the next template build.
    from .config import REPO_ROOT
    vm.put((REPO_ROOT / "template" / "files" / "sbx-recipe-run").read_bytes(),
           "/usr/local/bin/sbx-recipe-run", mode="0755", sudo=True)
    info(f"running the recipe {manifest.setup}")
    done = vm.run(f"sbx-recipe-run {q(proj_dir)} {q(manifest.setup)} {q(manifest.env_file)}",
                  check=False, capture=False)
    return done.code


def cmd_new(args, cfg: Config, runner: Runner, api=None) -> int:
    hostname = names.hostname(args.name)
    profile = args.profile or cfg.default_profile

    # Everything that can fail without a VM fails first.
    project, decisions = None, []
    if args.project:
        project = resolve_project(args.project, args.branch, args.from_path, runner)
        if project.manifest is not None:
            decisions = inputs_mod.plan(project.manifest, profile, project.checkout, dict(os.environ),
                                        _bindings(project).inputs, set(args.with_input), set(args.without_input))
            if decisions:
                print(inputs_mod.table(decisions))
        _preflight_git(cfg, runner, profile, project)
    elif args.with_input or args.without_input:
        raise InputError("--with and --without need --project")
    if args.remote_control_mode and profile != remotecontrol.ALLOWED_PROFILE:
        raise InputError(f"--remote-control-mode: Remote Control is not available in an {profile} sandbox")
    if not cfg.ssh_key_path.exists():
        raise ConfigError("no sbx SSH key; run `sbx setup` first")
    if args.gpu and not cfg.gpu_mapping:
        raise ConfigError("gpu_mapping is not set in config.toml")
    claude = "" if args.no_claude else claudetoken.get(runner)
    if not args.no_claude:
        if not claude:
            warn("no Claude token; Claude Code in this sandbox will ask you to sign in. Run: sbx claude-token")
        elif note := claudetoken.expiry_warning():
            warn(note)

    pve = _pve(cfg, runner, api)
    if pve.find(hostname):
        raise PveError(f"{hostname} already exists")
    built = pve.templates()
    tpl_name = args.template or _project_template(cfg, project, built)
    if not tpl_name:
        if not built:
            raise PveError("no template is built. Build one: sbx template rebuild <name> "
                           "(`sbx template list` shows the definitions)")
        raise InputError(f"more than one template is built ({', '.join(built)}); pass --template, "
                         "set [recipe] template in the project, or set default_template in config.toml")
    template = pve.template(tpl_name)
    if note := _stale_warning(template):
        warn(note)
    if args.gpu and (holder := pve.gpu_holder()):
        raise PveError(f"the GPU is held by VM {holder[0]} ({holder[1]}); run `sbx gpu detach` there first")

    ttl = args.ttl if args.ttl is not None else (cfg.agent_ttl_days if profile == "agent" else 0)
    expires = dt.date.today() + dt.timedelta(days=ttl) if ttl else None
    vmid = pve.next_vmid()
    info(f"cloning template {template.name} ({template.vm_name}) into VM {vmid} ({hostname}, {profile})")
    node = pve.create(template, vmid, hostname, profile, cfg.ssh_key_path.with_suffix(".pub").read_text(),
                      cores=args.cores or cfg.cores, memory_mb=args.memory or cfg.memory_mb,
                      disk_gb=args.disk, expires=expires, project=project.name if project else "")
    if args.gpu:
        pve.gpu_attach(node, vmid)
    pve.start(node, vmid)

    # A reused name must not trip over the previous VM's host key.
    runner.run(["ssh-keygen", "-R", cfg.fqdn(hostname), "-f", str(state_dir() / "known_hosts")], check=False)

    info("waiting for the sandbox")
    vm = _connect(cfg, runner, pve, node, vmid, hostname)
    vm.run("cloud-init status --wait >/dev/null 2>&1 || true")
    _refresh_shell_files(vm)

    https = _install_cert(cfg, runner, vm)
    if claude:
        # Before the recipe and the 'clean' snapshot: a rollback keeps the sign-in.
        _install_claude(vm, claude)
        info("Claude Code is signed in to your Claude subscription")
    code = 0
    if project is not None:
        try:
            code = _setup_project(vm, project, decisions, _git_auth(cfg, runner, vm, profile, project), profile)
        except (CommandError, VmError) as exc:
            # The VM exists; a bare error would leave the user with a dead end.
            raise VmError(f"{exc}\n  The sandbox {hostname} is up without its project. "
                          f"Fix the cause, then: sbx rm {args.name} -y && sbx new {args.name} ...") from None

    if code != 0:
        warn(f"the recipe failed with exit code {code}. The sandbox stays up: sbx ssh {args.name}")
    elif profile == "agent":
        # After the recipe, so a rollback returns to a VM that is ready for work.
        info("taking the 'clean' snapshot")
        pve.snapshot(node, vmid, "clean")

    if not args.no_herdr:
        _herdr_add(runner, hostname)
        if project is not None and code == 0:
            # After the recipe: the panes start the servers the recipe set up.
            _layout_build(runner, vm, hostname, project.name)
    rc_mode = "" if args.no_remote_control else (args.remote_control_mode or _rc_mode(cfg, profile))
    if rc_mode and code == 0:
        try:
            _remote_control_setup(cfg, runner, vm, hostname, profile, project, rc_mode)
        except (CommandError, VmError, InputError) as exc:
            warn(f"Remote Control not set up: {exc}. Later: sbx remote-control {args.name}")

    scheme = "https" if https else "http"
    print(f"\n{hostname} is up ({profile}, template {template.name}, VM {vmid}"
          + (f", expires {expires}" if expires else "") + ")")
    print(f"  ssh    sbx ssh {args.name}        (or: ssh {hostname})")
    print(f"  herdr  in your herdr sidebar     (a full window: sbx herdr {args.name} --attach)")
    print(f"  web    {scheme}://{cfg.fqdn(hostname)}:<port>")
    return 1 if code else 0


def _refresh_shell_files(vm: Vm) -> None:
    """The dev user's zshenv and zshrc, and the port mirror, from THIS
    checkout, over the template's copies. The template is a cache of the
    slow parts; these files are cheap, and a fix to them must reach the next
    sandbox at once, the way the recipe runner does, not wait for a template
    build. The mirror is restarted only when its file changed."""
    from .config import REPO_ROOT
    files = REPO_ROOT / "template" / "files"
    for name in ("zshenv", "zshrc"):
        vm.put((files / name).read_bytes(), f".{name}", mode="0644")
    lib = "/usr/local/lib/sbx/sbx_mirror.py"
    vm.put((files / "sbx_mirror.py").read_bytes(), lib + ".new", mode="0644", sudo=True)
    vm.run(f"sudo sh -c 'if cmp -s {lib}.new {lib}; then rm -f {lib}.new; "
           f"else mv {lib}.new {lib} && systemctl restart sbx-mirror; fi'", check=False)


def _connect(cfg: Config, runner: Runner, pve: Pve, node: str, vmid: int, hostname: str) -> Vm:
    """By name as soon as dnsmasq has the name; by address until then.

    The name comes from the DHCP request that carries the final hostname, which
    the sandbox sends after cloud-init. When the guest agent reports an address
    first, the sandbox is asked for that lease at once. The Mac's own resolver
    is asked only AFTER dnsmasq has the name: an earlier query would plant a
    negative answer that the Mac keeps for about 75 s.
    """
    deadline = time.monotonic() + 240
    fqdn = cfg.fqdn(hostname)
    by_address: Vm | None = None
    kicked = False
    while time.monotonic() < deadline:
        if dns_has(cfg.dns_server, fqdn):
            # A stale negative answer from an earlier sandbox of the same name
            # can still sit in the Mac's cache; wait it out.
            for _ in range(30):
                if resolves(fqdn):
                    vm = Vm(cfg, runner, hostname)
                    vm.wait(max(30.0, deadline - time.monotonic()))
                    return vm
                time.sleep(3)
            break
        if by_address is None and (address := pve.guest_ipv4(node, vmid)):
            by_address = Vm(cfg, runner, hostname, address)
        if by_address is not None and not kicked:
            try:
                by_address.wait(60.0)
                # cloud-init must have set the hostname before the lease is asked for.
                by_address.run("cloud-init status --wait >/dev/null 2>&1; sudo systemctl start sbx-dhcp-hostname", check=False)
                kicked = True
            except VmError:
                pass
        time.sleep(3)
    if by_address is None:
        raise VmError(f"VM {vmid} got no address in 240 s; open its console in the Proxmox web UI")
    warn(f"{fqdn} does not resolve on this Mac; using {by_address.address}. "
         "Check that Tailscale is connected and split DNS is set.")
    return by_address


def cmd_list(args, cfg: Config, runner: Runner, api=None) -> int:
    today = dt.date.today()
    rows = [("NAME", "PROFILE", "TEMPLATE", "PROJECT", "STATUS", "VMID", "EXPIRES", "ADDRESS")]
    for box in _pve(cfg, runner, api).sandboxes():
        exp = box.expires
        rows.append((box.hostname, box.profile, box.template or "-", box.project or "-", box.status, str(box.vmid),
                     "-" if exp is None else f"{exp}{' (EXPIRED)' if exp < today else ''}",
                     cfg.fqdn(box.hostname)))
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    for r in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip())
    if note := claudetoken.expiry_warning():
        warn(note)
    return 0


def cmd_ssh(args, cfg: Config, runner: Runner, api=None) -> int:
    hostname = names.hostname(args.name)
    box = _pve(cfg, runner, api).require(hostname)
    # The agent is forwarded to a personal sandbox only. A full-permission
    # agent could use the forwarded key for as long as the session lasts.
    argv = Vm(cfg, runner, hostname).ssh_argv(forward_agent=box.profile == "personal", tty=not args.command)
    # Joined with spaces, as ssh itself does: `sbx ssh x -- ls code` runs
    # `ls code`, and a quoted 'a; b' stays one argument that the remote shell
    # splits. shlex.join would turn that into one word, a command not found.
    os.execvp("ssh", argv + (["--", " ".join(args.command)] if args.command else []))


def _herdr_add(runner: Runner, hostname: str) -> None:
    """Best effort: put the sandbox in the local herdr's sidebar."""
    if not herdr_mod.available():
        return
    result = herdr_mod.add(runner, hostname)
    if result == "added":
        info(f"{hostname} is in your herdr sidebar")
    elif result != "present":
        warn(f"could not add {hostname} to herdr: {result}. Try: sbx herdr {hostname[4:]}")


def _layout_build(runner: Runner, vm: Vm, hostname: str, project_name: str, *,
                  run: bool = True, replace: bool = False, strict: bool = False) -> bool:
    """Builds the project's `.sandbox/herdr.toml` panes in the sandbox's herdr.

    Best effort from `sbx new` (strict=False): a layout is a convenience, and
    a failure names its cause and leaves the sandbox up. `sbx layout` is
    strict: its whole point is the layout.
    """
    def fail(text: str) -> bool:
        if strict:
            raise ConfigError(text)
        warn(f"{text}. Later: sbx layout {hostname[4:]}")
        return False

    clone = f"code/{project_name}"
    got = vm.run(f"cat {shlex.quote(clone + '/.sandbox/herdr.toml')} 2>/dev/null", check=False)
    if got.code != 0 or not got.stdout.strip():
        if strict:
            raise ConfigError(f"{project_name} has no .sandbox/herdr.toml in the sandbox")
        return False
    if not herdr_mod.available():
        return fail("herdr is not installed on this Mac, so the panes were not built")
    if not herdr_mod.find(runner, hostname):
        return fail(f"{hostname} is not a herdr machine, so the panes were not built")
    # herdr takes a remote pane's cwd as given: `~/code/x` opens in `~`.
    home = vm.run('printf %s "$HOME"').stdout.strip() or "/home/dev"
    try:
        lay = layout_mod.parse(got.stdout)
        h = layout_mod.Herdr(runner, hostname)
        ids = layout_mod.build(h, lay, f"{home}/{clone}", run=run, replace=replace)
        problems = layout_mod.check(h, lay, ids)
    except layout_mod.LayoutError as exc:
        return fail(f"herdr panes: {exc}")
    if problems:
        warn("the herdr panes are not the grid the layout asked for:\n    " + "\n    ".join(problems))
    info(f"herdr tab '{lay.tab}' with {len(ids)} panes is ready in {hostname}"
         + ("" if run else "; the commands are typed, not started"))
    return True


def cmd_layout(args, cfg: Config, runner: Runner, api=None) -> int:
    hostname = names.hostname(args.name)
    box = _pve(cfg, runner, api).require(hostname)
    if not box.project:
        raise ConfigError(f"{hostname} was made without --project, so it has no layout")
    if not herdr_mod.available():
        raise ConfigError("herdr is not installed on this Mac")
    if not herdr_mod.find(runner, hostname):
        _herdr_add(runner, hostname)
    vm = Vm(cfg, runner, hostname)
    _layout_build(runner, vm, hostname, box.project, run=not args.no_run, replace=args.replace, strict=True)
    return 0


def cmd_herdr(args, cfg: Config, runner: Runner, api=None) -> int:
    hostname = names.hostname(args.name)
    config = Path("~/.ssh/config").expanduser()
    if not config.exists() or SSH_INCLUDE not in config.read_text():
        raise ConfigError("herdr connects through your SSH config; run `sbx setup --mac-only` and add the Include line")
    if args.attach:
        # One full window on the sandbox alone, the way `herdr --remote` works.
        os.execvp("herdr", ["herdr", "--remote", hostname])
    if not herdr_mod.available():
        raise ConfigError("herdr is not installed on this Mac")
    result = herdr_mod.add(runner, hostname)
    if result == "added":
        info(f"{hostname} is in your herdr sidebar; open herdr and pick it there")
    elif result == "present":
        info(f"{hostname} is in your herdr sidebar already; open herdr and pick it there")
    else:
        raise ConfigError(f"herdr could not add {hostname}: {result}")
    return 0


def cmd_snap(args, cfg: Config, runner: Runner, api=None) -> int:
    pve = _pve(cfg, runner, api)
    box = pve.require(names.hostname(args.name))
    pve.snapshot(box.node, box.vmid, args.label)
    info(f"snapshot '{args.label}' taken")
    return 0


def cmd_rollback(args, cfg: Config, runner: Runner, api=None) -> int:
    pve = _pve(cfg, runner, api)
    box = pve.require(names.hostname(args.name))
    pve.rollback(box.node, box.vmid, args.label)
    info(f"rolled back to '{args.label}'")
    return 0


def _remove(cfg: Config, runner: Runner, pve: Pve, box) -> None:
    info(f"destroying {box.hostname} (VM {box.vmid}) and its snapshots")
    pve.destroy(box.node, box.vmid)
    runner.run(["ssh-keygen", "-R", cfg.fqdn(box.hostname), "-f", str(state_dir() / "known_hosts")], check=False)
    shutil.rmtree(state_dir() / "certs" / box.hostname, ignore_errors=True)
    if herdr_mod.remove(runner, box.hostname):
        info(f"{box.hostname} left your herdr sidebar")


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() == "y"
    except EOFError:
        # No terminal to answer on: that is a no, not a traceback.
        print("\nno answer: stopped (pass -y to proceed without a question)")
        return False


def cmd_rm(args, cfg: Config, runner: Runner, api=None) -> int:
    pve = _pve(cfg, runner, api)
    box = pve.require(names.hostname(args.name))
    if not _confirm(f"Destroy {box.hostname} (VM {box.vmid}, {box.profile})?", args.yes):
        return 1
    _remove(cfg, runner, pve, box)
    return 0


def cmd_gc(args, cfg: Config, runner: Runner, api=None) -> int:
    pve = _pve(cfg, runner, api)
    today = dt.date.today()
    expired = [b for b in pve.sandboxes() if b.expires and b.expires < today]
    if not expired:
        print("no expired sandbox")
        return 0
    for box in expired:
        print(f"  {box.hostname}  expired {box.expires}")
    if not _confirm(f"Destroy {len(expired)} sandbox(es)?", args.yes):
        return 1
    for box in expired:
        _remove(cfg, runner, pve, box)
    return 0


def cmd_gpu(args, cfg: Config, runner: Runner, api=None) -> int:
    if not cfg.gpu_mapping:
        raise ConfigError("gpu_mapping is not set in config.toml")
    pve = _pve(cfg, runner, api)
    holder = pve.gpu_holder()
    if args.action == "status":
        print(f"GPU mapping {cfg.gpu_mapping}: " + (f"held by VM {holder[0]} ({holder[1]})" if holder else "free"))
        return 0
    box = pve.require(names.hostname(args.name))
    if args.action == "attach":
        if holder and holder[0] != box.vmid:
            raise PveError(f"the GPU is held by VM {holder[0]} ({holder[1]})")
        pve.stop(box.node, box.vmid)
        pve.gpu_attach(box.node, box.vmid)
    else:
        pve.stop(box.node, box.vmid)
        pve.gpu_detach(box.node, box.vmid)
    pve.start(box.node, box.vmid)
    info(f"GPU {args.action} done; {box.hostname} restarted")
    return 0


# --- entry ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sbx", description="Throwaway Proxmox dev sandboxes. `sbx guide` is the one-screen introduction.")
    p.add_argument("-v", "--verbose", action="store_true", help="print every command that runs")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("guide", help="the basic usage of sbx on one screen (also: sbx with no command)")
    s.set_defaults(fn=cmd_guide)

    s = sub.add_parser("setup", help="the one-time setup of a Proxmox host and this Mac, step by step")
    s.add_argument("--host", help="the address of the Proxmox host")
    s.add_argument("--mac-only", action="store_true",
                   help="set up this Mac only, for a host that `sbx setup` set up already")
    s.set_defaults(fn=hostsetup.cmd_setup)

    s = sub.add_parser("doctor", help="check the setup: the config, the API token, DNS, the tailnet path")
    s.add_argument("--isolation", action="store_true",
                   help="also make one sandbox per profile and prove what each can reach (about 2 min)")
    s.set_defaults(fn=doctor.cmd_doctor)

    def project_opts(sp, required):
        if required:
            sp.add_argument("project", help="a local checkout or a git URL")
        else:
            sp.add_argument("--project", help="a local checkout or a git URL to clone and set up")
        sp.add_argument("--branch", help="branch to clone (default: the checkout's branch)")
        sp.add_argument("--from", dest="from_path", metavar="PATH",
                        help="local checkout that file inputs come from, when --project is a URL")

    s = sub.add_parser("inputs", help="show what a project's recipe asks for; makes nothing")
    project_opts(s, required=True)
    s.set_defaults(fn=cmd_inputs)

    s = sub.add_parser("template", help="the base templates: list, new, rebuild, and more")
    ts = s.add_subparsers(dest="action", required=True)
    t = ts.add_parser("list", help="each definition, and whether its template is built and current")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("show", help="one template: its components, its build settings, its versions")
    t.add_argument("name")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("components", help="the components that a definition can list")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("new", help="start your own definition in templates/local/")
    t.add_argument("name")
    t.add_argument("--from", dest="from_name", metavar="TEMPLATE", help="start from this definition")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("export", help="write a template as one file to share: its definition and its own components")
    t.add_argument("name")
    t.add_argument("-o", "--output", metavar="FILE", help="write to this file (default: stdout)")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("import", help="read a template file into templates/local/ (a path, an https URL, or -)")
    t.add_argument("source", metavar="FILE|URL")
    t.add_argument("--as", dest="as_name", metavar="NAME", help="import it under this name")
    t.add_argument("--force", action="store_true", help="replace a definition or a component of the same name")
    t.add_argument("-y", "--yes", action="store_true", help="do not ask; the files are still printed")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("rebuild", help="build a new version of templates (15-40 min each)")
    t.add_argument("names", nargs="*", metavar="NAME")
    t.add_argument("--all", action="store_true", help="every definition")
    t.add_argument("--changed", action="store_true", help="each definition whose template is not current")
    t.add_argument("--no-versions", action="store_true", help="do not add the versions that the projects need")
    t.add_argument("-y", "--yes", action="store_true")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("finish", help="attach to a build that is running on the host (a dropped session)")
    t.add_argument("name")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("prune", help="remove the old template versions that no sandbox uses")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("rm", help="remove every version of a template that no sandbox uses")
    t.add_argument("name")
    t.add_argument("-y", "--yes", action="store_true")
    t.set_defaults(fn=cmd_template)
    t = ts.add_parser("adopt", help="make a template from before named templates a version of <name>")
    t.add_argument("vmid", type=int)
    t.add_argument("name")
    t.set_defaults(fn=cmd_template)

    s = sub.add_parser("versions", help="the Ruby, Node, Go and Docker images the projects need, per template")
    s.add_argument("--write", action="store_true", help="save them for the next build of each template")
    s.set_defaults(fn=cmd_versions)

    s = sub.add_parser("projects", help="the projects this Mac has used, their tokens and sandboxes")
    s.set_defaults(fn=cmd_projects, action="list")
    s = sub.add_parser("project", help="add or forget a project (see also: sbx projects)")
    ps = s.add_subparsers(dest="action", required=True)
    p_add = ps.add_parser("add", help="record a checkout, so --project <name> works by name")
    p_add.add_argument("project", help="a checkout path or a git URL")
    p_rm = ps.add_parser("rm", help="forget a project; its token and bindings stay")
    p_rm.add_argument("project", help="the project name")
    s.set_defaults(fn=cmd_projects)

    s = sub.add_parser("git-token", help="store a project's git token in the keychain and bind it")
    project_opts(s, required=True)
    s.add_argument("--host", default="github.com", help="the git host the token is for")
    s.add_argument("--username", default="x-access-token", help="the user name sent with the token")
    s.add_argument("--stdin", action="store_true", help="read the token from stdin instead of a hidden prompt")
    s.add_argument("--no-check", action="store_true", help="store without asking the git host about it")
    s.add_argument("--remove", action="store_true", help="forget the token and the binding")
    s.set_defaults(fn=cmd_git_token)

    s = sub.add_parser("claude-token", help="sign Claude Code in every sandbox in to your Claude subscription")
    s.add_argument("sandboxes", nargs="*", metavar="NAME",
                   help="also write the token into these sandboxes, which have none yet")
    how = s.add_mutually_exclusive_group()
    how.add_argument("--stdin", action="store_true", help="read a new token from stdin; no `claude setup-token`")
    how.add_argument("--push", action="store_true", help="no new token: write the stored one into the sandboxes")
    how.add_argument("--status", action="store_true", help="show whether a token is stored, and its expiry")
    how.add_argument("--remove", action="store_true", help="forget the token, and delete it from every sandbox")
    s.set_defaults(fn=cmd_claude_token)

    s = sub.add_parser("new", help="make a sandbox")
    s.add_argument("name")
    s.add_argument("--profile", choices=("agent", "personal"))
    s.add_argument("--template", metavar="NAME", help="the template to clone (default: the project's, then default_template)")
    project_opts(s, required=False)
    s.add_argument("--with", dest="with_input", action="append", default=[], metavar="INPUT",
                   help="send this input to an agent sandbox (repeatable)")
    s.add_argument("--without", dest="without_input", action="append", default=[], metavar="INPUT",
                   help="withhold this input; its placeholder is used (repeatable)")
    s.add_argument("--cores", type=int)
    s.add_argument("--memory", type=int, metavar="MB")
    s.add_argument("--disk", type=int, metavar="GB", help="grow the disk to this size")
    s.add_argument("--ttl", type=int, metavar="DAYS", help="0 = never expires")
    s.add_argument("--gpu", action="store_true", help="give this sandbox the host GPU (one VM at a time)")
    s.add_argument("--no-herdr", action="store_true", help="do not add the sandbox to your herdr sidebar")
    s.add_argument("--no-claude", action="store_true",
                   help="do not sign Claude Code in to your subscription (see: sbx claude-token)")
    s.add_argument("--no-remote-control", action="store_true", help="no Claude Code Remote Control server (personal only)")
    s.add_argument("--remote-control-mode", metavar="MODE",
                   help="permission mode of the Remote Control sessions (default: by profile, from config.toml)")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("remote-control", help="sign a sandbox in to claude.ai and run its Remote Control server")
    s.add_argument("name")
    s.add_argument("--mode", metavar="MODE", help="permission mode of the sessions (acceptEdits, bypassPermissions, ...)")
    s.add_argument("--off", action="store_true", help="stop and disable the server")
    s.add_argument("--status", action="store_true", help="the server's state")
    s.set_defaults(fn=cmd_remote_control)

    s = sub.add_parser("list", help="every sandbox")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("ssh", help="open a shell, or run a command after --")
    s.add_argument("name")
    s.add_argument("command", nargs="*")
    s.set_defaults(fn=cmd_ssh)

    s = sub.add_parser("herdr", help="put the sandbox in your herdr sidebar (or --attach: a full window on it)")
    s.add_argument("name")
    s.add_argument("--attach", action="store_true", help="one full herdr window on this sandbox alone")
    s.set_defaults(fn=cmd_herdr)

    s = sub.add_parser("layout", help="build the project's .sandbox/herdr.toml panes in the sandbox's herdr")
    s.add_argument("name")
    s.add_argument("--replace", action="store_true", help="close the tab of the same name first")
    s.add_argument("--no-run", action="store_true", help="type each command, do not start it")
    s.set_defaults(fn=cmd_layout)

    for name, fn, text in (("snap", cmd_snap, "take a snapshot"), ("rollback", cmd_rollback, "return to a snapshot")):
        s = sub.add_parser(name, help=text)
        s.add_argument("name")
        s.add_argument("label", nargs="?", default="clean")
        s.set_defaults(fn=fn)

    s = sub.add_parser("rm", help="destroy a sandbox and its snapshots")
    s.add_argument("name")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("gc", help="destroy every expired sandbox")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_gc)

    s = sub.add_parser("gpu", help="move the host GPU between sandboxes")
    s.add_argument("action", choices=("attach", "detach", "status"))
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=cmd_gpu)
    return p


def main(argv: list[str] | None = None, runner: Runner | None = None, api=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["guide"]
    args = build_parser().parse_args(argv)
    if args.cmd == "gpu" and args.action != "status" and not args.name:
        print("sbx: error: gpu attach/detach needs a sandbox name", file=sys.stderr)
        return 2
    try:
        cfg = load_config()
        return args.fn(args, cfg, runner or Runner(verbose=args.verbose), api) or 0
    except (ConfigError, ManifestError, InputError, names.NameError_, PveError, VmError, CommandError,
            projects_mod.ProjectError, templates_mod.TemplateError) as exc:
        print(f"sbx: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
