# The sidecar rollout

Where the `sidecar-prototype` branch stands, what remains to prove, and the
order to prove it in. [security.md](security.md) has the trust model,
[architecture.md](architecture.md) the mechanism, [reference.md](reference.md)
the settings.

## Where it stands

Every agent sandbox gets a sidecar: a small trusted VM on a VLAN that only
the two share. The sidecar holds the sandbox's credentials and its port
policy, translates port 22 and the open ports to the VM, and registers the
sandbox's name. `sbx new`, `sbx rm`, `sbx gc`, `sbx ssh --sidecar`,
`sbx claude-token --push` and `sbx doctor --isolation` know about it.
`agent_sidecar = false` in `config.toml` restores the flow from before.

Proved:

- `tests/run-sidecar-test.sh` (Docker, network namespaces): the firewall, the
  VLAN wiring, the credential proxy, the expose API, port 22 by DNAT, with a
  control for every refusal. Passed on 2026-09-26. Its first run found that a
  VM id cannot be a VLAN tag; `Config.vlan_for` came from that.
- The unit suite: what `sbx new` sends to Proxmox and over SSH, the two
  host-side scripts with stub commands, the sidecar's handlers, the setup
  wizard's order of steps.

## On a real host, 2026-09-26

A fresh Proxmox VE 9.2 host, set up from this branch by `sbx setup` on a
Debian machine (not a Mac: see [setup.md](setup.md#a-linux-machine-instead-of-a-mac)).
Each item below was open before; each is now proved.

1. **The bare template build.** `sbx template rebuild sidecar` builds the
   provision script's bare mode and the `sidecar` component on the Ubuntu
   image.
2. **The sidecar registers the sandbox's name.** The sandbox is reached by
   name, at its sidecar's address, from the tailnet.
3. **The sandbox's static wire address, resolver and route.** It reaches the
   internet through its sidecar and the gateway, and nothing else.
   `sbx doctor --isolation`: 19 ok, 0 failed, with the new rows "agent
   reaches its sidecar" and "agent cannot reach the gateway".
4. **The VLAN-aware bridge.** `10-bridges.sh` appended the stanza and ran
   `ifreload -a`; the LAN stayed up, `vmbr77` has no IPv6 address, and
   `vlan_filtering` is 1.
5. **Claude Code through the proxy with a subscription token.** One prompt
   in an agent sandbox answered; the sidecar logged
   `POST /v1/messages?beta=true -> 200`, and the sandbox held only the
   placeholder. `sidecar_claude` is `proxy` by default from here on.

Also proved there: a web server in an agent sandbox (port 4400), reached from
a tailnet device at the sandbox's name, through the gateway, the sidecar and
the VLAN.

What the host found, each fixed with a test:

- **"open" mode took the sidecar's own ports.** Its DNAT of 1024 to 32767
  sent 2222 (the sidecar's sshd) and 8081 (the expose API) to the VM, so
  `sbx ssh --sidecar` failed, and the sandbox could have answered in place
  of the approval API. The prerouting chain now returns those two ports
  first, the expose API refuses them, and the Docker test has an "open mode"
  section; without the fix, two of its checks fail.
- **`sidecar.py` did not listen until DNS gave up.** `HTTPServer` asks DNS
  for its own name before it listens; with no resolver in reach, the ports
  refused connections meanwhile. It showed in the Docker test on a Linux
  host (five wrong results) and would show on a sidecar that boots before
  the gateway answers. The server skips the lookup now.
- **Setup assumed a Mac.** Off macOS, the secrets are files in
  `~/.config/sbx/secrets/` ([the secret store](reference.md#the-secret-store)),
  and the local routes come from `ip -4 route`.

## What remains

- **A private repository through the sidecar** (step 5 below): a project
  with a git token, `sbx new app --project ...`; the clone goes through the
  proxy and the token never enters the VM. It needs a repository and a token.
- **A long Claude session through the proxy.** The proxy buffers each answer,
  so an interactive session shows each reply only when it is complete. Try a
  real session; streaming is the fix if it is too slow.

## The order on a new host

From a checkout of this branch, with no `host/local.conf` and no
`~/.config/sbx`, so that the wizard proposes fresh values.

1. `bin/sbx setup --host <address>`. Watch the proposed LAN bridge. Say
   yes to the bridge stanza, to the templates, and to the sidecar template.
   Do the Tailscale steps when it pauses.
2. On the host, after the bridges: `ip -6 addr show vmbr77` shows no address,
   and `cat /sys/class/net/vmbr77/bridge/vlan_filtering` shows `1`.
3. `bin/sbx doctor --isolation`.
4. `bin/sbx new lab`, then `sbx ssh lab -- curl -sS https://example.com -o /dev/null && echo ok`,
   and `sbx ssh lab --sidecar -- sudo journalctl -u sbx-sidecar -n 20`.
5. A project with a private repository and a git token: `sbx new app --project ...`.
   `sbx ssh app -- cat ~/.git-credentials` shows the placeholder at the
   proxy, no token.
6. One Claude Code prompt in an agent sandbox (`claude -p`), and the
   sidecar's log line for it.

If a step fails on the host, the sandbox stays up for its log, and
`agent_sidecar = false` makes the next sandbox the old way while the cause is
found. Change the host's root password after the setup if it went through a
chat.

## After that

In the order they were discussed, none started:

- **`sbx ports <name>`**: list a sandbox's pending port requests, approve and
  deny them. Today "ask" mode needs a `curl` to the expose API.
- **The Tailscale steps through the API.** With an API access token, setup
  could merge the policy (validated, with the ETag), join the gateway with a
  one-time tagged auth key instead of a login URL, and add the split-DNS
  nameserver. The rollout did those three by hand, with the API.

- **A lab VLAN at home** for the Proxmox host and the gateway's LAN leg, so
  a gateway compromise lands away from the house devices.
- **Public apps behind Cloudflare Access.** An outbound tunnel from a
  sidecar, a hostname per sandbox, approved through the expose API. Tailscale
  Serve fronts loopback only, so Caddy on the sidecar sits in between.
- **Fork a sandbox and run a workflow in the fork.** A full clone from a
  snapshot with a fresh machine id, host keys and name; a prompt over SSH
  stdin into a tmux session; the child reports back through the Mac, since
  sandboxes no longer see each other.
- **Debian and mise.** The `apt` key and `image_url` exist already. Debian
  first with the same components, then a `mise` component that replaces
  `ruby`, `go`, `rust`, `python` and the rvm/nvm shell setup, then a smaller
  core.
- **Uploads through the proxy.** The proxy buffers whole requests; a
  `git push` with a chunked body needs streaming.
