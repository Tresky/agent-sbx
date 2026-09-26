# Security

This document answers one question: is it safe to give an AI agent full
permissions inside an sbx sandbox? It states what sbx protects, how, what it
does not protect, and how to prove it on your own setup.

[architecture.md](architecture.md) has the full detail of each mechanism.

## What sbx assumes

- **An agent sandbox is hostile.** The agent has root in the VM. It can change
  its own address, its MAC address, its routes and its firewall. It can read
  every file in the VM, including each secret that you send in.
- **A project branch is hostile.** An agent can edit `.sandbox/sandbox.toml`
  and `.sandbox/setup.sh` on its branch, and you may run `sbx new` from that
  branch later.
- **Your Mac, your Proxmox host and your tailnet admin are trusted.**

## What an agent sandbox can reach

| From an agent sandbox to | Result |
|---|---|
| its sidecar: the credential proxy, the expose API, a ping | permitted |
| the internet, through its sidecar | permitted |
| the gateway's DNS, through its sidecar | permitted |
| anything else on the gateway | refused |
| a host on your LAN, including the Proxmox host | refused |
| a device on your tailnet, including your Mac | refused |
| a personal sandbox | refused |
| another agent sandbox, or another sandbox's sidecar | refused |

A personal sandbox reaches the internet, the LAN and the sidecars. It does not
reach the tailnet. Your Mac reaches every sandbox by name; for an agent
sandbox the name is its sidecar, which passes port 22 and the forwarded ports
to the VM.

`sbx doctor --isolation` proves the main rows of this table on your setup. It
makes one sandbox in each profile. It runs the same probes in both, so each
refusal has a control that passes, and it removes the two sandboxes.
`tests/run-sidecar-test.sh` proves the sidecar rows in network namespaces, with
a second agent sandbox.

With `agent_sidecar = false` in `config.toml`, an agent sandbox is made the
way it was before sidecars: on the agent bridge alone, with its credentials
inside, and able to reach the other agent sandboxes on that bridge.

## The sidecar

Every agent sandbox has a sidecar: a small VM cloned from the `sidecar`
template, which is bare (nftables and one Python service, none of the core).
The sandbox VM has ONE network card, on a VLAN that the hypervisor tags for that
sandbox alone; the sidecar's first card is on that VLAN too, and its
second sits untagged on the agent bridge, where the gateway routes. Root in
the sandbox cannot change the tag, so the sidecar cannot be routed around.

The sidecar holds what the sandbox must not:

- **The real credentials.** The project's git token, and with
  `sidecar_claude = "proxy"` the Claude token. The sandbox gets one
  placeholder, made for it alone, which works only against its own sidecar,
  and which a leak makes useless anywhere else. The sidecar swaps it for the
  real token on each call, and logs the call.
- **The port policy.** Port 22 of the sidecar's address is the sandbox's, by
  DNAT. With `sidecar_ports = "open"` every port from 1024 to 32767 is too, so
  the mirror's ports are direct as before. With `"ask"` a port opens only
  after the sandbox asked its sidecar and you approved, from the gateway side.
- **The firewall of one sandbox.** From the sandbox: the two services, DNS to
  the gateway, and the internet. Nothing private, not even the gateway. From
  the shared network: the gateway side only, never another sidecar. The
  gateway's own rules stay as a second layer.

`sbx new` writes the policy, the secret and the tokens into the sidecar over
SSH stdin, before the sandbox is reached. The sidecar registers the sandbox's
name in DHCP with its own request, so the sandbox has no path to the gateway
at all.

## How the network is enforced

**The profile is the bridge.** An agent sandbox's network card sits on one
bridge, a personal sandbox's on another. The gateway's firewall keys on the
bridge that a packet arrives from. The hypervisor sets the bridge, and root in
the VM cannot change it. An address range or a MAC prefix would give no
protection, because root in the VM controls both.

**The bridges have no physical port**, and the Proxmox host holds no address
on them. The only way out of a sandbox subnet is the gateway container.

**The gateway container** (`sbx-gw`) is the security boundary. It runs only
dnsmasq, nftables and Tailscale, and it installs security updates by itself.

**Two guards close the path back into the tailnet.** The gateway is a
Tailscale subnet router, and a subnet route works in both directions. With a
default tailnet policy, a sandbox could open a connection to each device on
your tailnet. Two guards stop that, and each one works alone:

1. **nftables in the gateway** drops each new connection from a sandbox toward
   the tailnet, the LAN (agent), and the personal subnet (agent). The rules
   live in their own table, so Tailscale's own rules cannot undo them.
2. **The tailnet policy** accepts traffic from `autogroup:member` only: the
   devices of human users. That excludes traffic that arrives from a subnet
   route. So a receiving device drops a packet from a sandbox, even if guard 1
   fails.

**Warning:** a tailnet policy rule with `*` as its source undoes guard 2.
Tailscale defines `*` to include approved subnets. Keep `*` out of the source
of every rule.

The sandboxes do not run Tailscale. No Tailscale key or identity is in a
sandbox.

## How the host is protected

- **Setup** uses a root shell with your password. No root key is stored on
  your Mac.
- **Daily work** uses the Proxmox API with a narrow token. The token can:
  - manage the VMs in one pool;
  - clone and read the templates, but not change or destroy one;
  - allocate space on one storage;
  - attach a network card to the two sandbox bridges, and no other bridge.
- The last right protects the whole design. Even a compromised Mac cannot put
  an agent sandbox on your LAN bridge: Proxmox refuses it.
- Each Mac has its own token, so you can revoke one Mac without the others.
- The token is in the macOS keychain. `config.toml` holds only a command that
  prints it.
- The host's certificate is verified on each request, with the host CA or a
  fingerprint pin. With a pin, sbx checks the certificate before it sends the
  token.

`sbx doctor` fails if the token has a right outside this set.

## How secrets are handled

**Nothing goes into an agent sandbox by default.** Each `file` and `env` input
of a project needs `--with <name>` or `--without <name>` on `sbx new`. There is
no prompt, so a script cannot answer one wrongly.

**The manifest is untrusted.** A branch can edit it, so:

- The manifest can make an input stricter, never looser. Only you permit an
  input, with `--with`.
- An input's destination cannot leave the clone.
- sbx does not follow a symbolic link to a file. A branch could commit
  `config/master.key` as a link to your SSH key.
- The manifest never names a command or a path on your Mac. Only your own
  bindings file (`~/.config/sbx/bindings/`) can.
- A git URL must be a plain remote.

[projects.md](projects.md) has the full rules.

**How values travel.** A value goes over SSH, after the clone and before the
recipe. It is never on a command line, never in a temporary file on your Mac,
and never in cloud-init data (which stays readable on the host). sbx adds each
file it sends to `.git/info/exclude` in the clone, so an agent cannot commit a
secret by accident.

**Git access.**

- An agent sandbox clones with a token for ONE project. A fine-grained token
  that covers only that project's repositories limits what a leaked token can
  read.
- A personal sandbox uses your forwarded SSH agent for the clones only.
- sbx never puts one of your own SSH keys in a sandbox. Sandboxes use a key
  pair that `sbx setup` makes for them alone.

**Claude.**

- Both profiles get your long-lived Claude Code token. An agent can read it.
  It gives access to your subscription until it expires or you revoke it.
- Remote Control needs a full claude.ai sign-in, which can make API keys on
  your organization. So only a personal sandbox can get it. No option changes
  that.

**Certificates.** The mkcert CA key stays on your Mac. A sandbox gets a leaf
certificate for its own names only, so it cannot make a certificate that your
Mac trusts.

## The limits

- **Without a sidecar, agent sandboxes can reach each other.** With
  `agent_sidecar = false`, sandboxes on one bridge share a layer-2 segment
  that the gateway does not see. With sidecars, no two sandboxes share a
  segment.
- **A sidecar is trusted code that the sandbox can talk to.** Its API is
  small and fixed, and it runs no agent code, but it is reachable from a
  hostile VM. A compromise of a sidecar gives up that sandbox's credentials
  and its place on the sidecar network; the gateway's rules still hold.
- **A sandbox can claim another sandbox's name** with its DHCP request. It
  cannot show a valid certificate for that name, and SSH reports a changed
  host key.
- **A secret that you send in is readable by the agent.** That includes the
  project's git token and the Claude token. Send only what the task needs, and
  use tokens that you can revoke.
- **`--with <name>` permits an input by its name.** A branch can change the
  destination or the source path of that name within the checkout. Read the
  manifest diff of an agent branch before you run `sbx new` from it. The table
  that `sbx new` prints shows the real paths.
- **A snapshot contains the secrets on the disk.** `sbx rm` destroys the
  snapshots with the VM.
- **The internet is open.** An agent can send data anywhere on the internet.
  sbx protects your network, not your data from exfiltration.

## How to check your setup

1. Run `sbx doctor`. It checks the token scope, the DNS, and the path to the
   gateway.
2. Run `sbx doctor --isolation`. It proves the profile table above with two
   probe sandboxes.
3. In the Tailscale admin console, make sure that no policy rule has `*`, the
   gateway's tag, or a sandbox subnet as a source.
