# Reference

This document lists every command, every setting, and every file of sbx.
[usage.md](usage.md) explains how to use them together. A test
(`tests/test_docs.py`) keeps this list in step with the code.

## Commands

`<name>` is a sandbox name without the `sbx-` prefix. `<project>` is a checkout
path, a git URL, or a name that `sbx projects` lists.

### Setup and checks

| Command | What it does |
|---|---|
| `sbx guide` | the basic usage on one screen. `sbx` with no command does the same. |
| `sbx setup [--host H] [--mac-only]` | the one-time setup of a Proxmox host and this Mac, step by step. It is safe to run again. `--mac-only` sets up one more Mac for a host that is set up already, and does not change the host. |
| `sbx doctor [--isolation]` | the setup checks. `--isolation` also makes one sandbox in each profile, proves what each can reach, and removes them (about two minutes). |

### Sandboxes

| Command | What it does |
|---|---|
| `sbx new <name> [options]` | makes a sandbox. The options are below. |
| `sbx list` | every sandbox: name, profile, project, status, VM ID, expiry, address |
| `sbx ssh <name> [-- command]` | a shell in the sandbox, or one command |
| `sbx snap <name> [label]` | takes a snapshot. The default label is `clean`. |
| `sbx rollback <name> [label]` | returns to a snapshot. The default label is `clean`. |
| `sbx rm <name> [-y]` | destroys the sandbox and its snapshots |
| `sbx gc [-y]` | destroys each expired sandbox, after a confirmation |
| `sbx herdr <name> [--attach]` | adds the sandbox to the herdr sidebar. `--attach` opens one full herdr window on it. |
| `sbx layout <name> [--replace] [--no-run]` | builds the project's `.sandbox/herdr.toml` panes in the sandbox. `--replace` closes the tab of the same name first. `--no-run` types each command and does not start it. |
| `sbx gpu status` | which sandbox holds the GPU |
| `sbx gpu attach <name>` | moves the GPU to the sandbox, and restarts it |
| `sbx gpu detach <name>` | takes the GPU from the sandbox, and restarts it |

Options of `sbx new`:

| Option | Meaning |
|---|---|
| `--profile agent\|personal` | the profile. The default is `default_profile` in `config.toml`. |
| `--project <project>` | clone this project and run its recipe |
| `--branch <b>` | the branch to clone. The default is the checkout's branch. |
| `--from <checkout>` | the checkout that `file` inputs come from, when `--project` is a URL |
| `--with <input>` | send this input to an agent sandbox. Repeatable. |
| `--without <input>` | withhold this input; its placeholder is used. Repeatable. |
| `--cores N` | CPU cores. The default is `cores` in `config.toml`. |
| `--memory MB` | memory. The default is `memory_mb` in `config.toml`. |
| `--disk GB` | grow the disk to this size |
| `--ttl DAYS` | days until the sandbox expires. `0` means no expiry. |
| `--gpu` | give this sandbox the host GPU |
| `--no-herdr` | do not add the sandbox to the herdr sidebar |
| `--no-claude` | do not send the Claude token |
| `--no-remote-control` | no Claude Remote Control server (personal only) |
| `--remote-control-mode MODE` | the permission mode of the Remote Control sessions |

### Projects and tokens

| Command | What it does |
|---|---|
| `sbx projects` | the projects that this Mac has used: git token, checkout, sandboxes |
| `sbx project add <project>` | records a checkout, so `--project <name>` works by name |
| `sbx project rm <name>` | forgets a project. Its token and its bindings stay. |
| `sbx inputs <project> [--branch B] [--from PATH]` | shows what the recipe asks for, and where each value comes from. It makes nothing. |
| `sbx git-token <project> [--host H] [--username U] [--stdin] [--no-check] [--remove]` | stores the project's git token in the keychain, after a check that it covers each repository. `--no-check` skips the check. `--remove` forgets it. |
| `sbx claude-token` | runs `claude setup-token` and stores the Claude token |
| `sbx claude-token --stdin` | reads a new Claude token from stdin |
| `sbx claude-token --push [name ...]` | writes the stored token into running sandboxes, and into the named ones |
| `sbx claude-token --status` | shows whether a token is stored, and when it expires |
| `sbx claude-token --remove` | forgets the token, and deletes it from each sandbox |
| `sbx remote-control <name> [--mode M] [--status] [--off]` | signs a personal sandbox in to claude.ai and runs its Remote Control server |

### The template

| Command | What it does |
|---|---|
| `sbx versions [--write]` | the Ruby and Node versions, Go versions and Docker images that the projects need, and what the template caches. `--write` puts the list in `host/local.conf`. |
| `sbx template status` | the template, what the next build caches, and its linked clones |
| `sbx template rebuild [--rm-sandboxes] [--no-versions] [-y]` | builds the template again (35 to 40 minutes). `--rm-sandboxes` destroys each sandbox first. `--no-versions` keeps `host/local.conf` as it is. |
| `sbx template finish` | attaches to a build that is running on the host, after a dropped SSH session |

`-v` before any command prints each process that sbx starts.

## The settings

sbx reads three layers. A later layer wins.

1. `host/defaults.conf`: the shared defaults, in git.
2. `host/local.conf`: the values of one setup, not in git. `sbx setup` writes
   it. `SBX_LOCAL_CONF` names a different file.
3. `~/.config/sbx/config.toml`: this Mac's own settings. `SBX_CONFIG_DIR`
   names a different directory.

The host scripts read layers 1 and 2 only. A shared key (the column "Mac key"
below) can therefore not be set in `config.toml` to a value that differs from
layer 2: sbx stops with an error.

### Host settings (`host/defaults.conf`, `host/local.conf`)

| Key | Mac key | Default | Meaning |
|---|---|---|---|
| `SBX_DOMAIN` | `domain` | `sbx.internal` | the DNS zone of the sandboxes. Never a `.local` name. |
| `SBX_LAN_BRIDGE` | `lan_bridge` | `vmbr0` | the bridge of your LAN |
| `SBX_AGENT_BRIDGE` | `agent_bridge` | `vmbr77` | the bridge of the agent subnet |
| `SBX_PERSONAL_BRIDGE` | `personal_bridge` | `vmbr78` | the bridge of the personal subnet |
| `SBX_AGENT_NET` | `agent_net` | `10.77.0` | the first three octets of the agent /24. The gateway is `.1`. |
| `SBX_PERSONAL_NET` | `personal_net` | `10.78.0` | the first three octets of the personal /24 |
| `SBX_GW_CTID` | `gw_ctid` | `9001` | the ID of the gateway container |
| `SBX_GW_HOSTNAME` | `gw_hostname` | `sbx-gw` | the host name of the gateway |
| `SBX_GW_LAN_IP` | | `dhcp` | the gateway's LAN address: `dhcp`, or an address with its prefix |
| `SBX_GW_LAN_GW` | | | the router, when `SBX_GW_LAN_IP` is static |
| `SBX_GW_STORAGE` | | `local-lvm` | the storage of the gateway's disk |
| `SBX_GW_TEMPLATE_STORAGE` | | `local` | the storage of the container template |
| `SBX_UPSTREAM_DNS` | | `1.1.1.1 9.9.9.9` | the DNS servers that the gateway asks |
| `SBX_DHCP_LEASE` | | `1h` | the DHCP lease time |
| `SBX_TAILSCALE_TAG` | `tailscale_tag` | `tag:sbx-gw` | the Tailscale tag of the gateway |
| `SBX_TEMPLATE_VMID` | `template_vmid` | `9000` | the ID of the template |
| `SBX_VMID_MIN` | `vmid_min` | `9100` | the first sandbox ID |
| `SBX_VMID_MAX` | `vmid_max` | `9199` | the last sandbox ID |
| `SBX_VM_STORAGE` | `vm_storage` | `local-lvm` | the storage of the VM disks. It must make linked clones. |
| `SBX_SNIPPET_STORAGE` | | `local` | a file storage for the cloud-init snippet of the build |
| `SBX_IMAGE_STORAGE_DIR` | | `/var/lib/vz/template/iso` | where the build keeps the Ubuntu image |
| `SBX_POOL` | `pve_pool` | `sbx` | the Proxmox pool of the sandboxes. The token is scoped to it. |
| `SBX_GPU_MAPPING` | `gpu_mapping` | | the name of a PCI Resource Mapping. Empty refuses `--gpu`. |
| `SBX_VM_USER` | `vm_user` | `dev` | the user in each sandbox |
| `SBX_TEMPLATE_CORES` | | `8` | the cores of the build VM |
| `SBX_TEMPLATE_MEMORY_MB` | | `8192` | the memory of the build VM |
| `SBX_TEMPLATE_DISK_GB` | | `60` | the disk of the template |
| `SBX_UBUNTU_IMAGE_URL` | | Ubuntu 24.04 cloud image | the base image |
| `SBX_RUBY_VERSIONS` | `template_ruby` | `3.4.10` | the Rubies that the template caches. Full versions only. Empty = no Ruby. |
| `SBX_NODE_VERSIONS` | `template_node` | `lts/*` | the Node versions that the template caches |
| `SBX_DOCKER_IMAGES` | `template_images` | | the Docker images that the template pulls |
| `SBX_GO_VERSION` | | `1.27.1` | the Go version. Empty = no Go. |
| `SBX_TEMPLATE_EXTRAS` | | | the optional parts, from `template/extras/`: `odin`, `gis` |
| `SBX_EXTRA_APT_PACKAGES` | | | more apt packages for the template |
| `SBX_ODIN_VERSION` | | `dev-2025-11` | the Odin version of the `odin` extra |
| `SBX_WGPU_VERSION` | | `v27.0.2.0` | the wgpu-native version of the `odin` extra |
| `SBX_PREMAKE_VERSION` | | `5.0.0-beta8` | the premake version of the `odin` extra |

### Mac settings (`~/.config/sbx/config.toml`)

| Key | Default | Meaning |
|---|---|---|
| `pve_api` | | `https://<host>:8006`. `sbx setup` writes it. |
| `pve_token_command` | | a command (a list of strings) that prints the API token. `sbx setup` writes a keychain command. |
| `pve_ca_file` | | the host's CA file. Exactly one of this and `pve_fingerprint` is set. |
| `pve_fingerprint` | | the SHA-256 fingerprint of the host's certificate |
| `pve_ssh` | `root@<pve_api host>` | the root shell for `sbx setup` and `sbx template rebuild` |
| `pve_ssh_options` | `["-o", "PubkeyAuthentication=no"]` | more `ssh` options for that shell |
| `default_profile` | `agent` | the profile of `sbx new` |
| `cores` | `8` | the cores of a new sandbox |
| `memory_mb` | `6144` | the memory of a new sandbox |
| `agent_ttl_days` | `3` | the expiry of a new agent sandbox. `0` = none. |
| `git_token_command` | | a command that prints a git token for EVERY project. Prefer one token per project. |
| `git_token_host` | `github.com` | the host of that token |
| `remote_control_mode` | `acceptEdits` | the permission mode of Remote Control sessions. `""` turns the server off. |
| `ssh_key` | `~/.config/sbx/id_ed25519` | the private key for the sandboxes |

The shared keys of the table above (`domain`, `agent_bridge`, and so on) are
also accepted, but only with the same value as in `host/local.conf`.

### Project bindings (`~/.config/sbx/bindings/<project>.toml`)

| Section | Keys | Meaning |
|---|---|---|
| `[inputs.<name>]` | exactly one of `command`, `path`, `value` | where the value of an input comes from on this Mac |
| `[git]` | `token_command`, `host`, `username` | the project's git token for agent sandboxes. `sbx git-token` writes it. |

[projects.md](projects.md) explains the manifest (`.sandbox/sandbox.toml`) and
the pane layout (`.sandbox/herdr.toml`).

## Files

### On the Mac, in `~/.config/sbx/`

| File | What it is |
|---|---|
| `config.toml` | the Mac settings |
| `id_ed25519`, `id_ed25519.pub` | the key pair for the sandboxes only. None of your own keys goes into a sandbox. |
| `ssh_config` | the `Host sbx-*` block that `~/.ssh/config` includes |
| `known_hosts` | one entry for each sandbox |
| `pve-root-ca.crt` | the host's CA, when `pve_ca_file` names it |
| `certs/sbx-<name>/` | the certificate of each sandbox |
| `bindings/` | the project bindings |
| `projects.toml` | the project registry |
| `claude-token.toml` | the date of the Claude token. The token itself is in the keychain. |

### In the macOS keychain

| Item | What it is |
|---|---|
| `sbx-pve-token` | the Proxmox API token of this Mac |
| `sbx-git-<project>` | the git token of one project |
| `sbx-claude-token` | the Claude Code token |

### In a sandbox

| Path | What it is |
|---|---|
| `~/code/<project>` | the project clone |
| `~/.local/state/sbx/recipe.log` | the output of the recipe |
| `~/.local/state/sbx/remote-control.log` | the output of the Remote Control server |
| `~/.config/sbx/claude.env` | the Claude token, read by every shell |
| `/etc/sbx/tls/` | the sandbox's certificate |
| `/etc/sbx/mirror.toml` | optional: the ports that the port mirror skips or forces |
| `/usr/local/bin/sbx-recipe-run` | runs a recipe: `sbx-recipe-run <dir> <script> [env-file]` |

### Proxmox tags on a sandbox

| Tag | Meaning |
|---|---|
| `sbx` | the VM is a sandbox |
| `sbx-agent`, `sbx-personal` | the profile |
| `sbx-exp-YYYYMMDD` | the expiry date |
| `sbx-proj-<project>` | the project |

### On the host

| Path | What it is |
|---|---|
| `/root/sbx/` | the copy of `host/`, `gw/` and `template/` that the setup runs |
| `/root/sbx/host/local.conf` | the values of this setup. `sbx setup --mac-only` reads it. |
