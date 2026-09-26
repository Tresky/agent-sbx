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
| `sbx ssh <name> [--sidecar] [-- command]` | a shell in the sandbox, or one command. `--sidecar`: the sandbox's sidecar instead (the sandbox's name, port 2222). |
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
| `--template NAME` | the template to clone. The default is the project's `[recipe] template`, then `default_template` in `config.toml`, then the only template that is built. |
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
| `sbx git-token <project> [--host H] [--username U] [--stdin] [--no-check] [--remove]` | stores the project's git token in [the secret store](#the-secret-store), after a check that it covers each repository. `--no-check` skips the check. `--remove` forgets it. The project may be a git URL: nothing is cloned, and no checkout is needed on this Mac. A project named by URL then reads its `.sandbox/` with this token. |
| `sbx claude-token` | runs `claude setup-token` and stores the Claude token |
| `sbx claude-token --stdin` | reads a new Claude token from stdin |
| `sbx claude-token --push [name ...]` | writes the stored token into running sandboxes, and into the named ones |
| `sbx claude-token --status` | shows whether a token is stored, and when it expires |
| `sbx claude-token --remove` | forgets the token, and deletes it from each sandbox |
| `sbx remote-control <name> [--mode M] [--status] [--off]` | signs a personal sandbox in to claude.ai and runs its Remote Control server |
| `sbx cloudflare-token [--stdin] [--remove]` | stores the Cloudflare API token that `sbx publish` uses, and sets `cloudflare_token_command` |
| `sbx publish <name> <port> [--policy P] [--host LABEL] [--plain]` | publishes a port of an agent sandbox at `https://<label>.<preview_zone>`, behind Cloudflare Access. The default label is `<name>-<port>`; the default policy is `me`. `--plain`: the server speaks plain http on `0.0.0.0`, though the sandbox has a certificate. See [Previews](usage.md#previews). |
| `sbx publish <name>` | lists what a sandbox publishes, and the policy of each |
| `sbx publish <name> <port> --off` | withdraws it (`--host LABEL --off` for a label of your own). The last one removes the tunnel too. |

### Templates

| Command | What it does |
|---|---|
| `sbx template list` | each definition and each built template: shared or local, current or out of date, the newest version, and the sandboxes that use it |
| `sbx template show <name>` | one template: its definition, components, build settings, fingerprint, versions and sandboxes |
| `sbx template components` | the components that a definition can list, with a description of each |
| `sbx template new <name> [--from TEMPLATE]` | writes `templates/local/<name>.toml`, empty or as a copy of another definition |
| `sbx template export <name> [-o FILE]` | writes the template as one file to share: its definition, the full text of its own components, and a hash of each shared component. The default is stdout. |
| `sbx template import <FILE\|URL\|-> [--as NAME] [--force] [-y]` | reads a template file into `templates/local/` and `template/components/local/`. It prints each file and asks first. `--as` imports under another name; `--force` replaces a definition or a component of the same name. A URL must be https. |
| `sbx template rebuild [NAME ...] [--all] [--changed] [--no-versions] [-y]` | builds a new version of each named template, of every definition (`--all`), or of each one that is not current (`--changed`). 15 to 40 minutes each. The old version stays for its sandboxes. `--no-versions` does not add the versions that the projects need. |
| `sbx template finish <name>` | attaches to a build that is running on the host, after a dropped SSH session |
| `sbx template prune` | removes the old versions that no sandbox uses |
| `sbx template rm <name> [-y]` | removes every version of a template that no sandbox uses. The definition stays. |
| `sbx template adopt <vmid> <name>` | makes a template from before named templates the first version of `<name>` |
| `sbx versions [--write]` | for each template: the Ruby and Node versions, Go versions and Docker images that its projects need, and what it caches. `--write` saves them for the next build. |

The commands that build or remove a template ask for the host's root
password one time. The others use the API token only.

`-v` before any command prints each process that sbx starts.

## The settings

sbx reads three layers. A later layer wins.

1. `host/defaults.conf`: the shared defaults, in git.
2. `host/local.conf`: the values of one setup, not in git. `sbx setup` writes
   it. `SBX_LOCAL_CONF` names a different file.
3. `~/.config/sbx/config.toml`: this Mac's own settings. `SBX_CONFIG_DIR`
   names a different directory.

The secrets are not in any layer: see [the secret store](#the-secret-store).

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
| `SBX_TEMPLATE_POOL` | `template_pool` | `sbx-templates` | the Proxmox pool of the templates. The token may clone and read them only. |
| `SBX_TEMPLATE_VMID_MIN` | `template_vmid_min` | `9000` | the first ID that a template version can take |
| `SBX_TEMPLATE_VMID_MAX` | `template_vmid_max` | `9099` | the last ID that a template version can take. An ID in use is skipped. |
| `SBX_VMID_MIN` | `vmid_min` | `9100` | the first sandbox ID |
| `SBX_VMID_MAX` | `vmid_max` | `9199` | the last sandbox ID |
| `SBX_VM_STORAGE` | `vm_storage` | `local-lvm` | the storage of the VM disks. It must make linked clones. |
| `SBX_SNIPPET_STORAGE` | | `local` | a file storage for the cloud-init snippet of the build |
| `SBX_IMAGE_STORAGE_DIR` | | `/var/lib/vz/template/iso` | where the build keeps the Ubuntu image |
| `SBX_POOL` | `pve_pool` | `sbx` | the Proxmox pool of the sandboxes. The token is scoped to it. |
| `SBX_GPU_MAPPING` | `gpu_mapping` | | the name of a PCI Resource Mapping. Empty refuses `--gpu`. |
| `SBX_VM_USER` | `vm_user` | `dev` | the user in each sandbox |
| `SBX_SIDECAR_LINK` | `sidecar_link` | `10.79.0` | the first three octets of the /30 wire between an agent sandbox and its sidecar: the sidecar is `.1`, the sandbox `.2`. Every pair uses it, on its own VLAN. |
| `SBX_SIDECAR_TEMPLATE` | `sidecar_template` | `sidecar` | the template definition that the sidecars are cloned from |
| `SBX_UBUNTU_IMAGE_URL` | | Ubuntu 24.04 cloud image | the image that each template starts from. A definition can name another. |

### Mac settings (`~/.config/sbx/config.toml`)

| Key | Default | Meaning |
|---|---|---|
| `pve_api` | | `https://<host>:8006`. `sbx setup` writes it. |
| `pve_token_command` | | a command (a list of strings) that prints the API token. `sbx setup` writes a command for [the secret store](#the-secret-store). |
| `pve_ca_file` | | the host's CA file. Exactly one of this and `pve_fingerprint` is set. |
| `pve_fingerprint` | | the SHA-256 fingerprint of the host's certificate |
| `pve_ssh` | `root@<pve_api host>` | the root shell for `sbx setup` and `sbx template rebuild` |
| `pve_ssh_options` | `["-o", "PubkeyAuthentication=no"]` | more `ssh` options for that shell |
| `default_profile` | `agent` | the profile of `sbx new` |
| `default_template` | | the template of `sbx new` when neither `--template` nor the project names one. Empty: the only template that is built. `sbx setup` sets it. |
| `cores` | `8` | the cores of a new sandbox |
| `memory_mb` | `6144` | the memory of a new sandbox |
| `agent_ttl_days` | `3` | the expiry of a new agent sandbox. `0` = none. |
| `git_token_command` | | a command that prints a git token for EVERY project. Prefer one token per project. |
| `git_token_host` | `github.com` | the host of that token |
| `remote_control_mode` | `acceptEdits` | the permission mode of Remote Control sessions. `""` turns the server off. |
| `ssh_key` | `~/.config/sbx/id_ed25519` | the private key for the sandboxes |
| `agent_sidecar` | `true` | every agent sandbox gets a sidecar: a small trusted VM on the sandbox's own VLAN that holds its credentials and its port policy. `false`: an agent sandbox sits on the agent bridge alone, as before sidecars. |
| `sidecar_ports` | `open` | what a sidecar forwards to its sandbox. `open`: port 22 and every port from 1024 to 32767. `ask`: port 22, and a port only after the sandbox asked and you approved. |
| `preview_zone` | | the domain of the previews, a zone on your Cloudflare account. Use one of its own, not your main domain. |
| `cloudflare_account_id` | | the Cloudflare account of that zone |
| `cloudflare_token_command` | | a command that prints the Cloudflare API token. `sbx cloudflare-token` writes it. |
| `preview_session` | `336h` | how long a sign-in to a preview lasts (hours) |
| `sidecar_claude` | `proxy` | how Claude Code in an agent sandbox reaches Claude. `proxy`: the token stays in the sidecar; the sandbox gets a placeholder and a base URL. `direct`: the subscription token goes into the sandbox. The proxy buffers each answer, so a long one arrives whole. |

The shared keys of the table above (`domain`, `agent_bridge`, and so on) are
also accepted, but only with the same value as in `host/local.conf`.

### Template definitions (`templates/<name>.toml`, `templates/local/<name>.toml`)

A local definition wins over a shared one of the same name. [templates.md](templates.md)
explains them.

| Key | Default | Meaning |
|---|---|---|
| `description` | | one line for `sbx template list` |
| `components` | `[]` | the components, in order, from `template/components/` (or `template/components/local/`) |
| `apt` | `[]` | more apt packages |
| `cores`, `memory_mb` | `8`, `8192` | the size of the build VM, and of a sidecar cloned from a bare template. At least 1024 MB; 256 when bare. |
| `disk_gb` | `60` | the disk of the template. At least 20; 3 when bare (a disk cannot be smaller than its image). A sandbox can grow it with `--disk`. |
| `bare` | `false` | `true` skips the core (Docker, Node, Chrome, Claude Code, herdr, the mirror): `qemu-guest-agent`, `ca-certificates`, `curl`, `python3`, a user with a bash shell, and the components only. The `sidecar` definition is bare. |
| `image_url` | `SBX_UBUNTU_IMAGE_URL` | the cloud image to start from |
| `[<component>]` | | the settings of one listed component, or of `node` or `docker` |

The settings of each component. A setting `[ruby] versions` reaches the
component as `SBX_RUBY_VERSIONS`.

| Table | Key | Default | Meaning |
|---|---|---|---|
| `[node]` | `versions` | `["lts/*"]` | the Node versions (nvm); the first is the default. Every template has Node. |
| `[docker]` | `images` | `[]` | the Docker images to pull. Every template has Docker. |
| `[ruby]` | `versions` | `["3.4.10"]` | the Rubies (rvm), full versions only; the first is the default |
| `[go]` | `version` | `1.27.1` | the Go version |
| `[rust]` | `toolchains` | `["stable"]` | the rustup toolchains; the first is the default |
| `[rust]` | `components` | `"clippy rustfmt"` | more rustup components |
| `[python]` | `versions` | `["3.13"]` | the Pythons that uv caches |
| `[odin]` | `version`, `wgpu_version`, `premake_version` | see `template/components/odin.sh` | the Odin, wgpu-native and premake releases |

`rails`, `gis` and `media` take no settings.

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
| `claude-token.toml` | the date of the Claude token. The token itself is in the secret store. |
| `previews.toml` | who may open a preview: one `[policy.<name>]` per policy, with `emails` and `email_domains` only. `[policy.me]` is required; it is the default. |
| `secrets/` | the secret store off macOS: one file per secret, this user only (0700, the files 0600) |

### In the repository, not in git

| Path | What it is |
|---|---|
| `host/local.conf` | the host settings of this setup |
| `templates/local/*.toml` | your own template definitions |
| `templates/local/versions.toml` | what `sbx versions --write` found, per template |
| `template/components/local/*.sh` | your own components |

### The secret store

On macOS, the secrets are items in the keychain. On another system, which has
no keychain, each is a file in `~/.config/sbx/secrets/` with the same name,
readable by this user only. `SBX_SECRET_STORE` (`keychain` or `file`) names
the store instead of the platform. The settings name a command that prints a
secret, never the value.

| Item | What it is |
|---|---|
| `sbx-pve-token` | the Proxmox API token of this Mac |
| `sbx-git-<project>` | the git token of one project |
| `sbx-claude-token` | the Claude Code token |
| `sbx-cloudflare-token` | the Cloudflare API token of `sbx publish` |

### In a sandbox

| Path | What it is |
|---|---|
| `~/code/<project>` | the project clone |
| `~/.local/state/sbx/recipe.log` | the output of the recipe |
| `~/.local/state/sbx/remote-control.log` | the output of the Remote Control server |
| `~/.config/sbx/claude.env` | the Claude token, read by every shell; with `sidecar_claude = "proxy"`, the sidecar's base URL and the placeholder instead |
| `~/.git-credentials` | with a sidecar: the placeholder for the sidecar's proxy. Without: the project's git token. |
| `/etc/sbx/tls/` | the sandbox's certificate |
| `/etc/sbx/mirror.toml` | optional: the ports that the port mirror skips or forces |
| `/etc/sbx/template` | the template of the sandbox: its name, fingerprint and components |
| `/usr/local/bin/sbx-recipe-run` | runs a recipe: `sbx-recipe-run <dir> <script> [env-file]` |

### Proxmox tags on a sandbox

| Tag | Meaning |
|---|---|
| `sbx` | the VM is a sandbox |
| `sbx-agent`, `sbx-personal` | the profile |
| `sbx-exp-YYYYMMDD` | the expiry date |
| `sbx-proj-<project>` | the project |
| `sbx-tpl-<name>` | the template it was cloned from |

### Proxmox tags on a sidecar

A sidecar does not carry `sbx`, so `sbx list`, `sbx rm` and `sbx gc` never
take it for a sandbox. Its VM is named `<sandbox>-sc`.

| Tag | Meaning |
|---|---|
| `sbx-sidecar` | the VM is the sidecar of an agent sandbox |
| `sbx-of-<sandbox>` | the sandbox it serves. `sbx rm` destroys the two together; `sbx gc` removes a sidecar whose sandbox is gone. |
| `sbx-tpl-<name>` | the template it was cloned from |

### In a sidecar

| Path | What it is |
|---|---|
| `/etc/sbx/sidecar.env` | the sandbox's policy: the wire, the sandbox's name, `sidecar_ports`, the upstreams. `sbx new` writes it. |
| `/etc/sbx/sidecar/secret` | the per-sandbox placeholder that the sandbox presents |
| `/etc/sbx/sidecar/tokens` | `claude=` and `github=`: the real credentials |
| `/etc/nftables.conf` | the firewall, rendered from `sidecar/nftables.conf.tmpl` by `sbx-sidecar-apply` |
| `/usr/local/bin/sbx-sidecar-apply` | renders the firewall, registers the sandbox's name, restarts the service. `--claude-token` reads a new token from stdin; `--tunnel-token` the tunnel's connector token (empty: stop the tunnel). |
| `/etc/sbx/sidecar/cloudflared.env` | `TUNNEL_TOKEN=`: the connector token of the sandbox's preview tunnel, while it publishes |
| `sbx-cloudflared.service` | runs `cloudflared` for the previews, while the token file exists |
| `/usr/local/lib/sbx/sidecar.py` | the credential proxy (`<wire>:8080`) and the expose API (`8081` on both sides) |

### Proxmox tags on a template

| Tag | Meaning |
|---|---|
| `sbx-template` | the VM is a template of sbx |
| `sbx-tpl-<name>` | the template's name. A template with no such tag is from before named templates, and counts as `default`. |
| `sbx-h-<fingerprint>` | the fingerprint of what was built; `sbx template list` compares it with the definition |
| `sbx-building` | the version is still being built |

### On the host

| Path | What it is |
|---|---|
| `/root/sbx/` | the copy of `host/`, `gw/`, `template/`, `templates/`, `sidecar/` and `sbxlib/` that the setup and the builds run |
| `/root/sbx/host/local.conf` | the values of this setup. `sbx setup --mac-only` reads it. |
