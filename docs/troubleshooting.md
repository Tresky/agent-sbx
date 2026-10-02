# Troubleshooting

Start with `sbx doctor`. It checks the config, the token, the DNS, and the path
to the gateway. Each line that fails names the next step.

```
sbx doctor
sbx doctor --isolation     also proves the two profiles (one sandbox each; three VMs with the sidecar)
```

The tables below list the problems that `sbx doctor` does not explain.

## Setup

| Symptom | Cause | What to do |
|---|---|---|
| `Host key verification failed` or `Permission denied` from ssh | an agent or a script ran the command; SSH has no terminal to ask for the password | run `sbx setup` (or the ssh command) in your own Terminal window |
| `Too many authentication failures` | ssh offers each of your keys before the password | sbx adds `-o PubkeyAuthentication=no`; add it to your own `ssh` and `scp` commands too |
| Tailscale refuses the gateway's tag | the tailnet policy does not name the tag yet | merge the policy fragment first; `sbx setup` shows it |
| `Failed to exec "tailscale"` during `20-gw-create.sh --tailscale` | an earlier run made the gateway, but `gw/setup.sh` stopped before it installed Tailscale | run `sbx setup` again; it runs the gateway setup again. Or on the host: `bash /root/sbx/host/20-gw-create.sh`, and read why it failed |
| `Temporary failure resolving 'deb.debian.org'` during the gateway setup | the gateway copied the host's resolver, often Tailscale's `100.100.100.100`, which it cannot reach | run `sbx setup` again; the gateway now uses `SBX_UPSTREAM_DNS`. On a gateway made before this fix: `pct set <ctid> --nameserver "1.1.1.1 9.9.9.9"`, then `pct reboot <ctid>` |
| `the gateway cannot resolve names through ...` | the LAN blocks DNS to the upstream servers | set `SBX_UPSTREAM_DNS` in `host/local.conf` to servers the LAN allows (your router, for example) |
| `the container got no default route on lan0` | the host's LAN has no DHCP server | answer "no" to the DHCP question in `sbx setup`, or set `SBX_GW_LAN_IP` and `SBX_GW_LAN_GW` in `host/local.conf` |
| `no active storage holds VM disks` | no storage can make linked clones | add an LVM-thin, ZFS or directory storage in Proxmox |
| the template build stops with `PROVISION FAILED` | a step in `template/provision.sh` or in a component failed | the script prints the end of the log; run `bash /root/sbx/host/vm-diag.sh <vmid>` on the host for more. Fix the cause, then build again: the new build removes the failed VM |
| downloads in a sandbox or a build die partway: `Connection reset by peer`, or apt's `Ign:` on a package | a gateway made before the fix answered out-of-window replies with a RST | refresh the gateway: `bash /root/sbx/host/20-gw-create.sh` on the host. `pct exec <ctid> -- nft list chain inet sbx_guard input` counts the packets it now drops |
| the build log repeats `W: Tried to start delayed item ... but failed` | apt loops after one failed download; the network is fine | each apt run now stops after 30 minutes and is tried again, up to 3 times; to save the wait, stop the build and build again |
| the template build prints nothing for many minutes | a compile is quiet for minutes | wait; the script warns after 15 quiet minutes and stops after 45 |
| the SSH session dropped during the build | the build VM continues by itself | `sbx template finish <name>` |
| `no component '<x>'`, or `needs <y> before it` | the definition names a component that does not exist, or lists them in the wrong order | `sbx template components` lists them; correct the definition |
| `template '<x>' is not built` | `sbx new` needs a built template | `sbx template rebuild <x>` |
| `more than one template is built` | `sbx new` cannot choose | pass `--template`, set `[recipe] template` in the project, or set `default_template` in `config.toml` |
| `sbx doctor` says a template is older than its definition | the definition or a component changed after the build | `sbx template rebuild <name>`, when convenient; the old template still works |
| `sbx doctor` says a template is from before named templates | the setup predates named templates | `sbx template adopt <vmid> default` |
| `rvm install 3.4` fails | rvm cannot complete a partial Ruby version | write full versions, for example `3.4.1` |
| `'<key>' is a shared setting` | `config.toml` sets a value that `host/local.conf` owns | move the value to `host/local.conf`, and remove it from `config.toml` |

## Names and the network

| Symptom | Cause | What to do |
|---|---|---|
| a sandbox name does not resolve | Tailscale is off on your Mac, or the split-DNS nameserver is missing | connect Tailscale; in the admin console, add the nameserver `<agent-net>.1`, restricted to your domain |
| a name does not resolve right after you add the split-DNS nameserver; `dig @100.100.100.100` finds it | Tailscale on macOS updates the system's DNS only when it connects | turn Tailscale off and on again on your Mac |
| a new sandbox's name does not resolve for about a minute | your Mac cached a negative answer (about 75 s) from a lookup before the sandbox existed | wait; sbx itself never asks too early |
| `does not resolve on this Mac` during `sbx new` | the sandbox's name was late, and sbx used its address | the sandbox works; the name follows |
| `tailscale ping` says `via DERP`, and everything is slow | your router blocks UDP between your Mac's subnet and the host's subnet | permit UDP port 41641 between the two subnets |
| a removed sandbox's name still answers | a DHCP lease lives for one hour | wait, or make a new sandbox with that name; it takes the name at once |
| `tenant1.sbx-lab.<domain>` does not resolve | dnsmasq makes no wildcard names | use the sandbox's own name, and route by path or port |

## Making a sandbox

| Symptom | Cause | What to do |
|---|---|---|
| `403` from the Proxmox API | the token lacks a right, often after a template rebuild | on the host: `bash /root/sbx/host/40-api-token.sh --acl-only` |
| `no free VM id` | the ID range is full | `sbx gc`, or `sbx rm` a sandbox |
| a personal sandbox cannot clone a private repository | the sandbox sees only your forwarded SSH agent, and the key is only in a file | `ssh-add --apple-use-keychain ~/.ssh/<key>` (on Linux: `ssh-add ~/.ssh/<key>`); sbx checks this before it makes a VM |
| an agent sandbox cannot clone a private repository | the project has no git token, or the token misses a repository | `sbx git-token <project>`; it checks each repository |
| `<input>: pass --with <input> or --without <input>` | an agent sandbox needs a decision for each input | `sbx inputs <project>` lists them; pass one option for each |
| herdr hangs, or `could not add ... to herdr` | herdr waited for an answer | the sandbox works; run `sbx herdr <name>` again |
| `Every agent sandbox needs a sidecar` | the `sidecar` template is not built | `sbx template rebuild sidecar` |
| git `Authentication failed` in an agent sandbox, and an empty `~/.git-credentials` | a sidecar from before the proxy's 401 named Basic auth: git asked empty-handed twice and deleted the placeholder. Or the token misses the repository, or changed after `sbx new` | `sbx template rebuild sidecar`, or `sbx git-token <project>`; then make the sandbox again. The sidecar's log: `sbx ssh <name> --sidecar -- sudo journalctl -u sbx-sidecar` (look for `-> 401`) |
| `git push` of a large change fails in an agent sandbox | the proxy buffers, and a chunked upload does not pass | push in smaller pieces, or from your machine |
| `sbx rollback` warns `the sidecar ... stays as it is` | the sidecar has no snapshot of that label (a sandbox from before snapshots covered sidecars) | nothing to fix; take the next snapshot with `sbx snap` |

## Recipes

| Symptom | Cause | What to do |
|---|---|---|
| the recipe failed | see the log | `sbx ssh <name> -- tail -n 50 .local/state/sbx/recipe.log` |
| you fixed the recipe and want to run it again | | `ssh sbx-<name> sbx-recipe-run code/<project> .sandbox/setup.sh <env-file>` |
| `Could not find rails-...` after a green `bundle install` | the project has `.ruby-gemset`, and the gems went to another gemset | `rvm use <version>@<gemset> --create` before `bundle install` |
| a recipe dies on an unset variable | `set -u` breaks rvm and nvm | remove `-u` from the recipe's `set` line |
| a second run of the recipe fails | a step is not safe to repeat | see "Write a recipe that can run again" in [projects.md](projects.md) |

## Ports and HTTPS

| Symptom | Cause | What to do |
|---|---|---|
| the browser shows `http://` only, or a certificate warning | no mkcert CA on your Mac | `mkcert -install`, then make the sandbox again |
| `curl` says the certificate is not trusted | Homebrew's `curl` uses its own CA bundle | use `/usr/bin/curl`, or a browser |
| a server in Docker has no `https://` | it binds every interface, so sbx does not add TLS | use `http://`, or bind the server to `127.0.0.1` |
| a port does not answer | the port is outside 1024 to 32767, or it is 2019, 9222 or 9229 | use another port, or change `/etc/sbx/mirror.toml` in the sandbox |
| Rails refuses the request with "Blocked hosts" | the project overrides `config.hosts` | add `.<domain>` to `config.hosts` in development |
| with `sidecar_ports = "ask"`, a port of an agent sandbox does not answer | the port is not approved | `curl -d '{"port": N}' http://sbx-<name>.<domain>:8081/approve` ([usage.md](usage.md)) |

## Previews

| Symptom | Cause | What to do |
|---|---|---|
| Rails "Blocked hosts", or Vite "This host is not allowed", on a preview | the preview arrives with its public hostname | allow `.<preview_zone>`: `RAILS_DEVELOPMENT_HOSTS`, `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS` ([usage.md](usage.md#previews)) |
| a Cloudflare error page (502, Bad gateway) | nothing listens on that port in the sandbox; or a server on `0.0.0.0` with no TLS was published without `--plain`; or a new tunnel is still coming up | start the app; publish again with `--plain`; wait a minute |
| `sbx publish` says `the sidecar of ... has no cloudflared` | the sidecar's template is older than previews | `sbx template rebuild sidecar`, then make the sandbox again |
| `sbx publish` says `has no sidecar` | a personal sandbox, or `agent_sidecar = false` | only an agent sandbox with a sidecar can publish |
| `preview_zone ... is not set`, `no ... previews.toml`, `[policy.me] is missing`, or `cloudflare_token_command is not set` | the one-time setup is incomplete | `sbx cloudflare-setup` ([usage.md](usage.md#previews)) |
| `sbx cloudflare-setup`: `is pending, not active` | the domain's nameservers do not point to Cloudflare yet | set them at your registrar, wait, run it again |
| `sbx cloudflare-setup`: `Cloudflare One (Zero Trust) is not turned on` | the account has no Zero Trust organization | dashboard: Zero Trust, a team name and a plan |
| `sbx cloudflare-setup` ends with a `todo` for the login method | the token may not manage login methods | add One-time PIN by hand (Zero Trust, Settings, Authentication), or give the token that permission (`--token-guide`) |

## Claude Code

| Symptom | Cause | What to do |
|---|---|---|
| Claude Code in a sandbox is not signed in | the sandbox was made with `--no-claude`, or before a token existed | `sbx claude-token --push <name>` |
| a running session still uses the old token | a session reads the token when it starts | restart the session |
| `this is an API key` | `sbx claude-token` takes the subscription token | run `claude setup-token`, and paste its token |
| `sbx remote-control` says `is an agent sandbox` | an agent sandbox gets the full claude.ai login only on request | `sbx remote-control <name> --allow-agent`; the agent can then read the login |
| `Remote Control is only available when using Claude via api.anthropic.com` | `claude remote-control` was started in a shell of an agent sandbox, where the proxy's `ANTHROPIC_BASE_URL` is set | use `sbx remote-control`, which clears it |
| the Claude sessions did not come back after a host reboot | the sandbox is not marked | `sbx autostart <name>`; `sbx autostart <name> --status` shows what it would resume |

## The tests

| Symptom | Cause | What to do |
|---|---|---|
| a Docker test fails with `At least one invalid signature was encountered` | the Docker disk is full | `docker system df`, then free space |
