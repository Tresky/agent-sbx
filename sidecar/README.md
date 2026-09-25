# The sidecar

One trusted VM per agent sandbox. The sandbox VM has exactly one network link,
a VLAN that only it and its sidecar share, and the sidecar decides what
crosses it. The sidecars share a network with the gateway; the agents share
nothing.

`sbx new` clones a sidecar beside each agent sandbox and writes its policy.
This directory holds what runs inside a sidecar; `template/components/sidecar.sh`
installs it into the `sidecar` template (`templates/sidecar.toml`).
[docs/security.md](../docs/security.md) has the trust model,
[docs/architecture.md](../docs/architecture.md) the mechanism.

## What is here

| File | Installed as | What |
|---|---|---|
| `nftables.conf.tmpl` | `/usr/local/lib/sbx/sidecar-nftables.tmpl` | the sidecar's boundary. From the sandbox: the two services, a ping, DNS to the gateway, the internet. From the shared network: the gateway side only. Port 22 and the open or approved ports are translated to the VM. |
| `sidecar.py` | `/usr/local/lib/sbx/sidecar.py` | the credential proxy (`<wire>:8080`): the sandbox's placeholder in, the real Claude or git token out. The expose API (`8081`): the sandbox asks, only the net side approves, an approval adds the port to the nftables set `approved`. |
| `sbx-sidecar-apply` | `/usr/local/bin/sbx-sidecar-apply` | renders the firewall from `/etc/sbx/sidecar.env`, registers the sandbox's name in DHCP, starts the service. `--claude-token` replaces the Claude token from stdin. |
| `sbx-sidecar.service` | systemd | runs `sidecar.py` with the addresses that apply wrote |
| `sbx-sidecar-apply.service` | systemd | runs apply after cloud-init on each boot, because cloud-init sets the VM's own name back |

The sidecar's own sshd is on port 2222 (`sbx ssh <name> --sidecar`).

## What the tests prove

- `tests/test_sidecar.py`: the handlers, with in-memory sockets and a fake
  upstream. The real token goes upstream in the right header (a bearer with
  the OAuth beta header, or `x-api-key` for a Console key; basic auth for
  git). A wrong placeholder, no placeholder, or a bad basic-auth password is
  refused before any upstream call. Only the net side opens a port, and a
  refused firewall change is an error, not an approval.
- `tests/run-sidecar-test.sh` (Docker): two agents, two sidecars, the gateway
  with its real rules, a LAN host that is also the "internet", and a tailnet
  device, on a VLAN-aware bridge. An agent reaches its sidecar and the
  internet and nothing else, not even the other agent when it tries the
  bridge directly. Port 22 of the sidecar's address reaches the sandbox from
  the tailnet and from nowhere else. A port opens only after an approval and
  closes on a denial. Every refusal has a control.

## Limits

- **The Claude lane with a subscription token is unverified.** Claude Code
  documents the base-URL path for a Console API key. The proxy sends a
  subscription token as a bearer with the OAuth beta header, which is what
  Claude Code itself sends, but that has not been tried against the real API.
  `sidecar_claude` is `direct` until it is.
- **Uploads are buffered.** The proxy reads a request and a response whole
  before it forwards them. A `git push` with a chunked body does not work
  through it; a clone does.
- **The forwarded ports are plain TCP** to the VM, which does its own TLS
  through the mirror, as before. The leaf key therefore still lives in the VM.
