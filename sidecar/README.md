# The sidecar prototype

One trusted container per agent sandbox. The sandbox VM has exactly one
network link, a VLAN that only it and its sidecar share, and the sidecar
decides what crosses it. The sidecars share a network with the gateway; the
agents share nothing.

This directory is a prototype of the sidecar, and `tests/run-sidecar-test.sh`
proves its claims in network namespaces. Nothing here is wired into `sbx new`
yet. The design and its trust zones are in the architecture page that this
branch was made for.

## What a sidecar does

| Job | Where | What |
|---|---|---|
| Credential proxy | `agent0:8080` | The VM sends a per-sandbox placeholder. The sidecar swaps it for the real Claude or git token and forwards the call. `/github/<path>` goes to the git host; the rest to Claude. Every call is logged. |
| Expose API | `agent0:8081` and `net0:8081` | The VM asks for one of its ports (`POST /expose {"port": 4400}`). Only the trusted side may `POST /approve` or `/deny`. `GET /requests` lists the states. |
| Port forward | `net0:<port>` | After an approval, a TCP relay to the VM's port. Caddy with TLS takes this job in the real design; the relay is enough to prove the policy. |

`nftables.conf.tmpl` is the sidecar's boundary. From the VM: the two services,
a ping, DNS to the gateway, and the internet. Nothing private. From the shared
network: the gateway side only, never another sidecar.

## What the test proves

`tests/run-sidecar-test.sh` builds two agent VMs, two sidecars, the gateway
with its real rules, a LAN host that is also the "internet", and a tailnet
device that plays your Mac. The host's bridge is VLAN-aware, with one VLAN per
sandbox, as Proxmox does for `net0: ...,tag=<vlan>`.

- An agent reaches its sidecar and the internet, and nothing else: not the
  gateway, not the LAN, not the tailnet, not another sidecar, and not the
  other agent even when it tries the bridge directly.
- The real tokens reach the upstream; the placeholder never does. A wrong
  placeholder, no placeholder, or another sandbox's placeholder is refused.
  The placeholder sent straight to the internet is just a string.
- A port is unreachable until the trusted side approves. The sandbox cannot
  approve its own request. After a denial the port closes again.
- Each refusal has a control: with the sidecar rules deleted, the gateway's
  layer still stops the LAN and the tailnet; with the VLAN tag moved, the
  agents reach each other; with the gateway guard deleted too, every path is
  open, so the topology itself was whole.

```
tests/run-sidecar-test.sh
```

## What is not here yet

- **Proxmox.** `sbx new` does not clone a sidecar, tag the sandbox's NIC, or
  write the secret and the tokens into it. `host/10-bridges.sh` does not make
  the agent bridge VLAN-aware. Both need a real host to try.
- **DHCP and DNS.** The test uses static addresses. In the design the sidecar
  takes its `net0` address from the gateway's dnsmasq, and the VM either asks
  its sidecar or keeps a static `/30` that every pair can reuse.
- **TLS.** The forward is plain TCP. The real sidecar runs Caddy with the
  sandbox's leaf certificate, so the leaf key moves out of the VM.
- **The Claude credential.** The proxy injects a bearer token, or an API key
  with `--claude-header x-api-key`. The documented gateway path for Claude Code
  needs a Console API key; the subscription token from `claude setup-token`
  is not supported through a custom base URL.
