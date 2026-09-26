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

Not proved, because each needs a real host:

1. The bare template build (`sbx template rebuild sidecar`): the provision
   script's bare mode and the `sidecar` component on a real Ubuntu image.
2. The sidecar registering the sandbox's name: `hostnamectl` and
   `networkctl reconfigure` after cloud-init, and dnsmasq moving the name.
3. The sandbox's static wire address and resolver from cloud-init
   (`ipconfig0`, `nameserver`, `searchdomain`), and its route through the
   sidecar to the internet.
4. The VLAN-aware bridge stanza from `10-bridges.sh` on a real
   `/etc/network/interfaces`, and that `ifreload -a` keeps the LAN up.
5. Claude Code through the proxy (`sidecar_claude = "proxy"`) against the
   real API with a subscription token. Claude Code documents that path for a
   Console API key only. The default stays `direct` until this is tried.

## The order on the new host

A fresh Proxmox host at `172.16.3.231`, from a checkout of this branch. No
`host/local.conf` and no `~/.config/sbx` exist yet, so the wizard proposes
fresh values.

1. `bin/sbx setup --host 172.16.3.231`. Watch the proposed LAN bridge. Say
   yes to the bridge stanza (item 4), to the templates, and to the sidecar
   template (item 1). Do the Tailscale steps when it pauses.
2. On the host, after the bridges: `ip -6 addr show vmbr77` shows no address,
   and `cat /sys/class/net/vmbr77/bridge/vlan_filtering` shows `1`.
3. `bin/sbx doctor --isolation`. Items 2 and 3 are proved by the agent probe
   sandbox coming up at all: it is reached by name through its sidecar. The
   new rows are "agent reaches its sidecar" and "agent cannot reach the
   gateway".
4. `bin/sbx new lab`, then `sbx ssh lab -- curl -sS https://example.com -o /dev/null && echo ok`,
   `sbx ssh lab -- cat ~/.git-credentials` (the placeholder at the proxy, no
   token), and `sbx ssh lab --sidecar -- sudo journalctl -u sbx-sidecar -n 20`.
5. A project with a private repository and a git token: `sbx new app --project ...`.
   The clone goes through the sidecar; the token never enters the VM.
6. Item 5: `sidecar_claude = "proxy"` in `config.toml`, a new sandbox, and one
   Claude Code prompt inside it. If it fails with an authentication error,
   the proxy needs a Console API key for agent sandboxes, or the forward
   proxy design instead. Set the default from what happens.

If a step fails on the host, the sandbox stays up for its log, and
`agent_sidecar = false` makes the next sandbox the old way while the cause is
found. Change the host's root password after the setup: it went through a
chat.

## After that

In the order they were discussed, none started:

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
