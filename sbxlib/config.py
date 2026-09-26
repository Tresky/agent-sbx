"""Settings, lowest to highest: host/defaults.conf, host/local.conf, then
~/.config/sbx/config.toml. The first two are shared with the host scripts."""
from __future__ import annotations

import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_ENV = REPO_ROOT / "host" / "defaults.conf"
LOCAL_ENV = REPO_ROOT / "host" / "local.conf"


class ConfigError(ValueError):
    pass


def state_dir() -> Path:
    return Path(os.environ.get("SBX_CONFIG_DIR", "~/.config/sbx")).expanduser()


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=VALUE lines as the shell would read them. No variable expansion:
    defaults.conf promises plain literals so that bash and Python agree."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parts = shlex.split(value, comments=True)
        out[key.strip()] = parts[0] if parts else ""
    return out


@dataclass
class Config:
    pve_api: str = ""                  # e.g. "https://192.168.1.10:8006"
    # argv list that prints the API token as USER@REALM!TOKENID=SECRET. A
    # command, not a value: the secret stays in the keychain, not in a file.
    pve_token_command: list[str] = field(default_factory=list)
    # The host's certificate is self-signed. Exactly one of these verifies it.
    pve_ca_file: str = ""              # a copy of /etc/pve/pve-root-ca.pem
    pve_fingerprint: str = ""          # SHA-256, as the web UI shows it
    pve_pool: str = "sbx"              # the pool the token is scoped to
    # The root shell for the SETUP scripts only (a template rebuild). Never a
    # stored key: ssh asks for the password. Default: root at pve_api's host.
    pve_ssh: str = ""
    # -o PubkeyAuthentication=no: a Mac with many keys otherwise offers each
    # one and the host closes the connection before the password prompt.
    pve_ssh_options: list[str] = field(default_factory=lambda: ["-o", "PubkeyAuthentication=no"])
    # The fields from here to template_vmid_max mirror host/defaults.conf (a
    # test keeps them equal). A setup changes them in host/local.conf.
    domain: str = "sbx.internal"
    agent_bridge: str = "vmbr77"
    personal_bridge: str = "vmbr78"
    vmid_min: int = 9100
    vmid_max: int = 9199
    vm_user: str = "dev"
    agent_net: str = "10.77.0"         # the gateway answers DNS at .1
    personal_net: str = "10.78.0"
    gw_ctid: int = 9001
    gw_hostname: str = "sbx-gw"
    tailscale_tag: str = "tag:sbx-gw"
    lan_bridge: str = "vmbr0"
    vm_storage: str = "local-lvm"
    # What the template caches; `sbx versions` derives them from the projects.
    template_pool: str = "sbx-templates"
    template_vmid_min: int = 9000
    template_vmid_max: int = 9099
    sidecar_link: str = "10.79.0"       # the /30 wire of each pair: sidecar .1, VM .2
    sidecar_template: str = "sidecar"   # the template that a sidecar is cloned from
    # Every agent sandbox gets a sidecar: a trusted VM on the sandbox's own
    # VLAN that holds the real credentials and the port policy. False makes an
    # agent sandbox the way it was before sidecars: on the agent bridge alone.
    agent_sidecar: bool = True
    # What the sidecar forwards to its sandbox. "open": port 22 and every port
    # from 1024 to 32767, as the mirror exposes them. "ask": port 22, and a
    # port only after the sandbox asked for it and you approved.
    sidecar_ports: str = "open"
    # How Claude Code in an agent sandbox reaches Claude. "direct": the
    # subscription token goes into the sandbox, as before. "proxy": the token
    # stays in the sidecar, and the sandbox gets a placeholder and a base URL.
    # Claude Code documents the proxy path for a Console API key; the rollout
    # proved it with the subscription token too (docs/sidecar-rollout.md).
    sidecar_claude: str = "proxy"
    # The template that `sbx new` clones when neither --template nor the
    # project's manifest names one. Empty: the only template, if there is one.
    default_template: str = ""
    default_profile: str = "agent"
    cores: int = 8
    memory_mb: int = 6144
    agent_ttl_days: int = 3
    # argv list that prints a git token for agent sandboxes. Lives only here,
    # never in a manifest: a manifest must not be able to run a command.
    git_token_command: list[str] = field(default_factory=list)
    git_token_host: str = "github.com"
    gpu_mapping: str = ""              # name of a PCI Resource Mapping; empty = --gpu is refused
    # Claude Code Remote Control in a PERSONAL sandbox: "" = off, else the
    # permission mode of the server's sessions. An agent sandbox never gets it:
    # a full claude.ai login can make API keys on the organization.
    remote_control_mode: str = "acceptEdits"
    ssh_key: str = ""                  # private key for the VMs; default <state>/id_ed25519
    # `sbx publish`: previews behind Cloudflare Access (sbxlib/previews.py).
    # The zone is a domain of its own, not your main one: an agent serves what
    # it wants there. The token command prints a Cloudflare API token.
    preview_zone: str = ""
    cloudflare_account_id: str = ""
    cloudflare_token_command: list[str] = field(default_factory=list)
    preview_session: str = "336h"      # how long a sign-in lasts; Cloudflare's form, e.g. 24h

    @property
    def ssh_key_path(self) -> Path:
        return Path(self.ssh_key).expanduser() if self.ssh_key else state_dir() / "id_ed25519"

    def bridge_for(self, profile: str) -> str:
        return {"agent": self.agent_bridge, "personal": self.personal_bridge}[profile]

    def sidecar_for(self, profile: str) -> bool:
        """Whether a sandbox of this profile gets a sidecar."""
        return profile == "agent" and self.agent_sidecar

    def vlan_for(self, vmid: int) -> int:
        """The VLAN of a sandbox and its sidecar. A VLAN id is 1 to 4094 and a
        sandbox id is 9100 and up, so the id itself cannot be the tag: the
        tag is the sandbox's place in the id range, from 2 (1 is the untagged
        default of the bridge, where the sidecars and the gateway sit)."""
        return vmid - self.vmid_min + 2

    @property
    def sidecar_addr(self) -> str:
        return f"{self.sidecar_link}.1"

    @property
    def sidecar_vm_addr(self) -> str:
        return f"{self.sidecar_link}.2"

    @property
    def sidecar_proxy_url(self) -> str:
        return f"http://{self.sidecar_addr}:8080"

    @property
    def dns_server(self) -> str:
        return f"{self.agent_net}.1"

    def fqdn(self, hostname: str) -> str:
        return f"{hostname}.{self.domain}"

    @property
    def pve_ssh_target(self) -> str:
        if self.pve_ssh:
            return self.pve_ssh
        import urllib.parse
        host = urllib.parse.urlsplit(self.pve_api).hostname or ""
        if not host:
            raise ConfigError("pve_ssh is not set, and pve_api names no host to derive it from")
        return f"root@{host}"



_ENV_MAP = {
    "SBX_DOMAIN": "domain", "SBX_AGENT_BRIDGE": "agent_bridge", "SBX_PERSONAL_BRIDGE": "personal_bridge",
    "SBX_VMID_MIN": "vmid_min", "SBX_VMID_MAX": "vmid_max",
    "SBX_VM_USER": "vm_user", "SBX_AGENT_NET": "agent_net", "SBX_PERSONAL_NET": "personal_net",
    "SBX_GW_CTID": "gw_ctid", "SBX_GW_HOSTNAME": "gw_hostname", "SBX_TAILSCALE_TAG": "tailscale_tag",
    "SBX_LAN_BRIDGE": "lan_bridge", "SBX_VM_STORAGE": "vm_storage",
    "SBX_TEMPLATE_POOL": "template_pool", "SBX_TEMPLATE_VMID_MIN": "template_vmid_min",
    "SBX_TEMPLATE_VMID_MAX": "template_vmid_max", "SBX_POOL": "pve_pool", "SBX_GPU_MAPPING": "gpu_mapping",
    "SBX_SIDECAR_LINK": "sidecar_link", "SBX_SIDECAR_TEMPLATE": "sidecar_template",
}


def local_conf_path() -> Path:
    """host/local.conf, or $SBX_LOCAL_CONF. The tests point it away, so they
    see the shared defaults on every setup."""
    return Path(os.environ.get("SBX_LOCAL_CONF") or LOCAL_ENV)


def load(config_path: Path | None = None, defaults_path: Path = DEFAULTS_ENV,
         local_path: Path | None = None) -> Config:
    local_path = local_path or local_conf_path()
    cfg = Config()
    types = {name: f.type for name, f in Config.__dataclass_fields__.items()}

    def assign(key: str, value, origin: str):
        if key not in types:
            raise ConfigError(f"{origin}: unknown key '{key}'")
        if types[key] == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ConfigError(f"{origin}: '{key}' must be a whole number") from None
        elif types[key] == "list[str]":
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                raise ConfigError(f"{origin}: '{key}' must be a list of strings")
        elif types[key] == "bool":
            if not isinstance(value, bool):
                raise ConfigError(f"{origin}: '{key}' must be true or false")
        elif not isinstance(value, str):
            raise ConfigError(f"{origin}: '{key}' must be a string")
        setattr(cfg, key, value)

    for shared in (defaults_path, local_path):
        if shared.exists():
            for env_key, value in parse_env_file(shared.read_text()).items():
                if env_key in _ENV_MAP:
                    assign(_ENV_MAP[env_key], value, str(shared))

    path = config_path or state_dir() / "config.toml"
    if path.exists():
        shared_keys = {v: k for k, v in _ENV_MAP.items()}
        with open(path, "rb") as fh:
            for key, value in tomllib.load(fh).items():
                if key in shared_keys and value != getattr(cfg, key):
                    # The host scripts never read config.toml. A value set
                    # only here would put the Mac and the host out of step.
                    raise ConfigError(f"{path}: '{key}' is a shared setting; set {shared_keys[key]} "
                                      f"in host/local.conf instead, and remove it from config.toml")
                assign(key, value, str(path))

    if cfg.default_profile not in ("agent", "personal"):
        raise ConfigError("default_profile must be 'agent' or 'personal'")
    if cfg.sidecar_ports not in ("open", "ask"):
        raise ConfigError("sidecar_ports must be 'open' or 'ask'")
    if cfg.vlan_for(cfg.vmid_max) > 4094:
        raise ConfigError(f"the sandbox id range {cfg.vmid_min}-{cfg.vmid_max} is wider than the 4093 VLANs "
                          "that one bridge has; narrow SBX_VMID_MIN/SBX_VMID_MAX in host/local.conf")
    if cfg.sidecar_claude not in ("direct", "proxy"):
        raise ConfigError("sidecar_claude must be 'direct' or 'proxy'")
    if not re.fullmatch(r"[1-9][0-9]*h", cfg.preview_session):
        raise ConfigError("preview_session must be a number of hours, e.g. \"336h\"")
    return cfg
