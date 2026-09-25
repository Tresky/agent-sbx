# Troubleshooting

Start with `sbx doctor`. It checks the config, the token, the DNS, and the path
to the gateway. Each line that fails names the next step.

```
sbx doctor
sbx doctor --isolation     also proves the two profiles (makes two VMs)
```

The tables below list the problems that `sbx doctor` does not explain.

## Setup

| Symptom | Cause | What to do |
|---|---|---|
| `Host key verification failed` or `Permission denied` from ssh | an agent or a script ran the command; SSH has no terminal to ask for the password | run `sbx setup` (or the ssh command) in your own Terminal window |
| `Too many authentication failures` | ssh offers each of your keys before the password | sbx adds `-o PubkeyAuthentication=no`; add it to your own `ssh` and `scp` commands too |
| Tailscale refuses the gateway's tag | the tailnet policy does not name the tag yet | merge the policy fragment first; `sbx setup` shows it |
| `the container got no default route on lan0` | the host's LAN has no DHCP server | answer "no" to the DHCP question in `sbx setup`, or set `SBX_GW_LAN_IP` and `SBX_GW_LAN_GW` in `host/local.conf` |
| `no active storage holds VM disks` | no storage can make linked clones | add an LVM-thin, ZFS or directory storage in Proxmox |
| the template build stops with `PROVISION FAILED` | a step in `template/provision.sh` or in a component failed | the script prints the end of the log; run `bash /root/sbx/host/vm-diag.sh <vmid>` on the host for more. Fix the cause, then build again: the new build removes the failed VM |
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
| a personal sandbox cannot clone a private repository | the sandbox sees only your forwarded SSH agent, and the key is only in a file | `ssh-add --apple-use-keychain ~/.ssh/<key>`; sbx checks this before it makes a VM |
| an agent sandbox cannot clone a private repository | the project has no git token, or the token misses a repository | `sbx git-token <project>`; it checks each repository |
| `<input>: pass --with <input> or --without <input>` | an agent sandbox needs a decision for each input | `sbx inputs <project>` lists them; pass one option for each |
| herdr hangs, or `could not add ... to herdr` | herdr waited for an answer | the sandbox works; run `sbx herdr <name>` again |

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

## Claude Code

| Symptom | Cause | What to do |
|---|---|---|
| Claude Code in a sandbox is not signed in | the sandbox was made with `--no-claude`, or before a token existed | `sbx claude-token --push <name>` |
| a running session still uses the old token | a session reads the token when it starts | restart the session |
| `this is an API key` | `sbx claude-token` takes the subscription token | run `claude setup-token`, and paste its token |
| `sbx remote-control` refuses a sandbox | Remote Control is for personal sandboxes only | make a personal sandbox for Remote Control |

## The tests

| Symptom | Cause | What to do |
|---|---|---|
| a Docker test fails with `At least one invalid signature was encountered` | the Docker disk is full | `docker system df`, then free space |
