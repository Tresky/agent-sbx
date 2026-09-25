# Architecture

This document is for a maintainer, or for a reader who wants to understand
sbx well enough to change it. It explains each mechanism, why it has its
shape, the traps that the real system found, and what the tests prove.

For the security view of the same design, read [security.md](security.md).

## The shape of the system

```
                 your Mac
                 sbx CLI · herdr · browser · ssh
                          │
                          │  Tailscale: a subnet route to the two sandbox
                          │  subnets, and split DNS for <domain>
                          ▼
   Proxmox host ──── sbx-gw (container) ────── lan0: your LAN, the internet
                       │            │
                  sbxa0 <agent>.1   sbxp0 <personal>.1
                       │            │
                  agent bridge      personal bridge        (no physical port)
                       │            │
                agent sandboxes   personal sandboxes        (linked clones)
                       ▲
                 the template VM (sbx-base-*)
```

Three things on the host are not sandboxes. `sbx setup` makes them one time:

| Thing | What it is |
|---|---|
| `sbx-gw`, one unprivileged container | The DHCP and DNS server of the sandboxes, their NAT route to the internet, the firewall, and the Tailscale subnet router. It runs only dnsmasq, nftables and Tailscale, and installs security updates by itself. |
| `sbx-base-*`, the template VM | An Ubuntu 24.04 VM with every tool installed, built once, then converted to a Proxmox template. A sandbox is a linked clone of it. |
| Two bridges | Linux bridges with no physical port. The host holds no address on them, so a sandbox cannot reach the hypervisor. |

The `sbx` command on the Mac makes and destroys everything else. The default
numbers are in [reference.md](reference.md): subnets `10.77.0.0/24` and
`10.78.0.0/24`, bridges `vmbr77` and `vmbr78`, template `9000`, gateway
`9001`, sandboxes `9100` to `9199`, DHCP from `.50` to `.250` with one-hour
leases.

### Settings

`host/defaults.conf` holds every default, and `host/local.conf` holds the
values of one setup. The host scripts source the two files. The CLI
(`sbxlib/config.py`) parses the same two files as plain `KEY=VALUE` lines, so
the two sides cannot disagree. That is why every value in `defaults.conf`
must be a plain literal: the Python side does not expand variables.

`~/.config/sbx/config.toml` is the Mac's own layer. It may repeat a shared key
only with the same value; a different value is an error, because the host
scripts never read that file.

`tests/test_cli.py` checks that the defaults of the `Config` dataclass equal
`defaults.conf`. The tests point `SBX_LOCAL_CONF` at an empty file, so they
see the shared defaults on every setup.

## The two profiles: the profile is the bridge

An agent sandbox's network card sits on the agent bridge, a personal one's on
the personal bridge. The gateway's firewall keys on the interface that a
packet arrives on. The hypervisor sets the bridge, and root inside the VM
cannot change it. A root user controls its own address and MAC, so an address
range or a MAC prefix would give no protection.

The API token may attach a card to the two sandbox bridges only, so even the
CLI cannot put a sandbox on the LAN bridge.

## Names, with no configuration

A sandbox gets its address and its name in one step, from one program:

1. The sandbox asks for an address with DHCP. The request carries its host
   name, for example `sbx-lab`.
2. dnsmasq in the gateway hands out an address. dnsmasq is also the
   authoritative DNS server for `<domain>`, and it answers for every name in
   its lease table. So `sbx-lab.<domain>` exists from that moment.
3. The lease is keyed by the MAC address. A reboot gets the same address back.
   A request with a new host name moves the name.
4. When the sandbox is gone, the lease ends after one hour, and the name goes
   with it. A new sandbox with the same name takes it over at once.

The DHCP request is the registration. No record is written, and no address is
static.

**Why not mDNS, and why `.internal`.** The first design used `.local` and mDNS,
so that no DNS server was needed. Two facts ended it: macOS may try unicast
DNS first for a `.local` name with two labels, and mDNS is link-local, which
would force the sandboxes onto the LAN segment. `.internal` is reserved for
private use. `.local` cannot work with a DNS server at all, because macOS sends
every `.local` name to mDNS.

**The first DHCP request carries `ubuntu`,** the image's own host name,
because it leaves before cloud-init sets the name. `sbx-dhcp-hostname.service`
asks for the lease again after cloud-init, with `networkctl reconfigure` (a
plain `renew` repeats the old request). The unit must be
`WantedBy=cloud-init.target` and `After=cloud-final.service`. Its first
version was wanted by `multi-user.target`; that closed an ordering cycle,
because `cloud-final.service` runs after `multi-user.target`, and systemd
broke the cycle by a silent deletion of the unit's job. `sbx new` also asks
the sandbox for its lease when the name is late, so the CLI does not depend on
the unit.

**The Mac caches a negative DNS answer for about 75 seconds,** and dnsmasq's
NXDOMAIN carries no SOA record to shorten that. So the CLI never asks the
system resolver for a sandbox name before dnsmasq has it. It asks dnsmasq
directly with a raw DNS query (`dns_has` in `sbxlib/vm.py`), and only then uses
the system resolver.

## The path from the Mac, and the two guards

The gateway runs Tailscale as a subnet router and advertises the two sandbox
subnets. The Mac's Tailscale accepts the routes. Split DNS in the Tailscale
admin console sends `<domain>` to the gateway. Nothing on the Mac is set by
hand: no resolver file and no static route. The same URLs work at home and
away. The sandboxes do not run Tailscale. Source NAT stays on, so a sandbox
sees the gateway's address as the client, never a tailnet address.

**The reverse path.** A subnet route works in both directions unless something
stops it. The default gateway of each sandbox is the subnet router. Tailscale
documents `*`, as a policy source, as "all traffic originating from Tailscale
devices in your tailnet, any approved subnets and autogroup:shared", and the
default policy permits `*` to everything. So, with the defaults, a sandbox can
open a connection to each device in the tailnet. Two guards stop this, and
both are needed.

**Guard 1: nftables in the gateway** (`gw/nftables.conf.tmpl`). The rules live
in their own table. In nftables a drop in any base chain is final, and an
accept only ends the chain that it is in. Tailscale accepts forwarded traffic
toward `tailscale0` in its own chain, and that accept cannot undo a drop here.

```
table inet sbx_guard {
  set private4 { type ipv4_addr; flags interval;
    elements = { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 100.64.0.0/10 } }

  chain input {                       # what a sandbox may ask of the gateway itself
    type filter hook input priority -10; policy accept;
    iifname { "sbxa0", "sbxp0" } jump from_sandbox
  }
  chain from_sandbox {
    ct state established,related accept
    udp dport { 53, 67 } accept       # DNS and DHCP
    tcp dport 53 accept
    icmp type echo-request accept
    drop
  }
  chain forward {
    type filter hook forward priority -10; policy accept;
    meta nfproto ipv6 iifname { "sbxa0", "sbxp0" } drop
    meta nfproto ipv6 oifname { "sbxa0", "sbxp0" } drop
    ct state established,related accept                  # replies to what a tailnet device opened
    iifname { "sbxa0", "sbxp0" } oifname "tailscale0" drop  # no sandbox opens into the tailnet
    iifname "sbxa0" ip daddr @private4 drop              # agent: nothing private
    iifname "sbxa0" oifname "sbxp0" drop                 # agent: not the personal subnet
    iifname "sbxp0" ip daddr 100.64.0.0/10 drop          # personal: not the tailnet range
    iifname "lan0" oifname { "sbxa0", "sbxp0" } drop     # entry through the tailnet only
  }
}
table ip sbx_nat {
  chain postrouting { type nat hook postrouting priority 100; policy accept;
    oifname "lan0" ip saddr { <agent>.0/24, <personal>.0/24 } masquerade }
}
```

**Guard 2: the tailnet policy** (`tailscale/policy.example.hujson`). The one
accept rule has `autogroup:member` as its source: the devices of human users.
That excludes tagged nodes and traffic that arrives from a subnet route. No
rule names `*`, the gateway's tag, or a sandbox subnet as a source. A
receiving device therefore drops a packet from a sandbox, even if guard 1
fails. `sbx setup` renders the fragment with the setup's own subnets and tag.

## Direct ports, and HTTPS on the same port

A port is direct, because the address is routed and not translated. A packet
for `https://sbx-lab.<domain>:4400` reaches port 4400 of that sandbox with the
destination unchanged. Two sandboxes can both use 4400.

The one piece of work is inside the sandbox. A dev server binds to
`127.0.0.1`, which the network cannot reach, and it speaks plain HTTP. The
template's `sbx-mirror` service (`template/files/sbx_mirror.py`) does the rest:

- Every two seconds, it reads `/proc/net/tcp` and `tcp6` for listeners bound
  to loopback only.
- For each one, it opens the **same port number** on the sandbox's routed
  address. Linux permits the two binds, because the addresses differ.
- It sends one probe: an unknown HTTP method with a valid version. Every HTTP
  server answers that with a status line before it calls the application. A
  Python dev server answers a bad *version* with no status line, which is why
  the version must be valid.
- A port that answered HTTP goes through Caddy, loaded through its admin API
  with one server per port. Caddy adds TLS with the sandbox's certificate,
  sets `X-Forwarded-Proto`, and redirects plain `http://` on that port to
  `https://`. A plain TLS-to-TCP pipe would not do: Rails compares the
  `Origin` header with its own base URL, and every form POST would fail.
- Every other port gets a raw TCP forward.
- A listener on the wildcard address is reachable already. The mirror leaves
  it alone, so it gets no TLS. Docker publishes that way.
- Ports 2019 (the Caddy admin API), 9222 and 9229 (debug ports) are never
  mirrored, and only ports 1024 to 32767 are. `/etc/sbx/mirror.toml` can change
  the lists and force a port to HTTP or to raw.
- When the routed address changes, every raw listener is bound again.

**Routes.** A recipe may give the mirror a route: one path prefix of a
mirrored HTTP port goes to another local upstream, in Caddy, before the port's
own server sees the request. A drop-in under `/etc/sbx/mirror.d/` declares it
(the format is in [projects.md](projects.md)). The mirror reads every `*.toml`
there on each poll, so a route appears within two seconds, and a recipe
rewrites its own file on every run.

Why routes exist: a Rails app in development loads every ES module as its own
request, three hundred and more for one page, and vite_ruby proxies each one
through Rails' Rack middleware. Measured in a sandbox, that is 38 ms per
module and four at a time (puma's 2 workers × 2 threads), against 2 ms from
Vite itself: a page that was ready in 4.1 s and idle at 7.5 s. With the route,
the same page is ready in 0.3 s and idle at 1.2 s, keeps its same-origin URLs,
and `bin/rails s` and `bin/vite dev` run as on the Mac. The alternative,
vite_ruby's `skipProxy`, sends the browser to port 3036 itself and fails on
Vite 6, which answers CORS for localhost origins only.

The template also sets `RAILS_DEVELOPMENT_HOSTS=.<domain>` and
`__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=.<domain>` in `/etc/environment`, so
Rails and Vite accept the sandbox name as a `Host` header.

**The certificate.** `sbx new` runs `mkcert` on the Mac and makes a leaf
certificate for the sandbox's two names, the full one and the short one. The
CA's private key never leaves the Mac, so a sandbox cannot make a certificate
that the Mac trusts. The leaf goes into the sandbox over SSH, with the key
readable by Caddy's group only. `sbx rm` deletes the Mac's copy.

## The Mac talks to Proxmox with a scoped token

The CLI holds no root key. The setup runs host scripts in a root shell with a
password (`sbx setup` opens one shared SSH connection, so the password is
asked once). Daily work uses the Proxmox HTTP API (`sbxlib/pve.py`) with a
token that `host/40-api-token.sh` makes. The token belongs to the user
`sbx@pve`, which has four small roles:

| Path | Role | Rights |
|---|---|---|
| `/pool/<pool>` | SbxOperator | allocate, clone into, configure, start and stop, snapshot, destroy VMs in the pool; read the guest agent |
| `/vms/<template>` | SbxTemplateUser | clone the template and read it; not change or destroy it |
| `/storage/<vm storage>` | SbxStorage | allocate disk space |
| `/sdn/zones/localnetwork/<agent bridge>`, `<personal bridge>` | SbxBridge | attach a network card to those two bridges, and no other |
| `/mapping/pci/<mapping>` (optional) | SbxMapping | use the GPU mapping |

`sbx setup` makes one token for each Mac (`cli-<Mac name>`), so a rotation on
one Mac does not stop the others. `sbx doctor` reads `/access/permissions` and
fails when the token has a right on any other path.

- The token lives in the macOS keychain. `config.toml` holds a command that
  prints it.
- The host's certificate is self-signed, and the CLI refuses to run with no
  verification: either the host's CA file, or a SHA-256 fingerprint pin. With
  a pin, the CLI checks the certificate before it sends the request.
  `sbx setup` uses the CA when the host address is in the certificate, and the
  pin otherwise.
- The token sees only its pool, so a VM ID that another guest holds is
  invisible in the resource list. The CLI asks `/cluster/nextid` for each
  candidate ID instead.
- `qm destroy` removes every access entry on the destroyed VM's path. A
  template rebuild therefore runs `40-api-token.sh --acl-only` at its end.

## What `sbx setup` does

`sbxlib/hostsetup.py`:

1. It asks for the host, opens one SSH connection with a shared control
   socket, and runs `host/discover.sh` through it. That script prints JSON:
   the bridges, the storage, the guests, the routes, the addresses, the CA,
   the fingerprint and the certificate's names.
2. It reads the Mac's routes (`netstat -rn`) and proposes values for
   `host/local.conf`: the bridge of the default route, a free bridge pair, a
   subnet pair that collides with no route on either side, a free block of
   IDs, and a storage that can make linked clones. A value that
   `host/local.conf` has already stays. When the gateway exists, every current
   value stays.
3. It copies `host/`, `gw/` and `template/` to `/root/sbx/`, then runs each host
   script whose check fails: the bridges, the gateway, Tailscale, the
   template. It stops at each manual step in the Tailscale admin console.
4. It makes this Mac's token (`40-api-token.sh --emit`) and stores it in the
   keychain, or keeps the one in the keychain and applies the ACLs again.
5. It sets up the Mac and runs `sbx doctor`.

`--mac-only` skips steps 2 and 3: it copies `/root/sbx/host/local.conf` from
the host instead, so the second Mac and the host agree.

## What `sbx new` does

`sbx new lab --profile agent --project ~/code/app --with rails-master-key`

1. **Everything that can fail without a VM fails first.** The name, the
   project (its origin, branch and manifest), a decision for each input, the
   git access (for `personal`, the SSH agent must hold a key that GitHub
   accepts; for `agent`, the project's token must read each repository), the
   SSH key, the mkcert CA, and a free VM ID.
2. **Clone.** `POST /nodes/<node>/qemu/<template>/clone` with `newid`, `name`
   and `pool`. A template clones as a linked clone: seconds, not minutes.
3. **Configure.** The network card on the profile's bridge, cores, memory, the
   cloud-init user, `ip=dhcp`, the sandbox public key (URL-encoded inside the
   form body, which the API expects), and the tags `sbx`, the profile, the
   expiry and the project.
4. **Start**, and remove the old host key of that name from the CLI's own
   `known_hosts`.
5. **Connect.** Poll dnsmasq for the name. When the guest agent reports an
   address first, connect by address, wait for cloud-init, start the
   DHCP-hostname unit, and move to the name as soon as dnsmasq has it.
6. **Refresh.** `~/.zshenv`, `~/.zshrc` and `sbx_mirror.py` come from the
   checkout, over the template's copies, so a fix to them reaches the next
   sandbox without a template build. The mirror restarts only when its file
   changed.
7. **Certificate.** mkcert on the Mac, two files into the sandbox over SSH,
   then `caddy` and `sbx-mirror` restart.
8. **Claude Code.** The Claude token goes into `~/.config/sbx/claude.env`.
9. **Git access.** For `personal`, the SSH agent is forwarded for the clones.
   For `agent`, the project's token goes into `~/.git-credentials`, and git
   uses HTTPS for the host even for a URL in the SSH form.
10. **Clone the project** into `~/code/<project>`, with each `repo` input.
11. **Send the inputs** over the SSH channel, and add each path to
    `.git/info/exclude`.
12. **Run the recipe** with `sbx-recipe-run`.
13. **Snapshot** `clean`, for `agent`.
14. **herdr and the layout.** Add the sandbox to the herdr sidebar, and build
    the project's panes.
15. **Remote Control**, for `personal` with a terminal.

A plain sandbox takes 24 to 30 seconds on the reference host.

## SSH

`sbx setup` writes a `Host sbx-*` block (`~/.config/sbx/ssh_config`) with
`HostName %h.<domain>`, the sandbox key with `IdentitiesOnly`, the CLI's own
`known_hosts`, and `ForwardAgent no`. The CLI itself runs `ssh -F /dev/null`
with every option explicit, because that block also matches the full name and
would append the domain twice. `known_hosts` is keyed by the full name, and
the CLI's `HostKeyAlias` is the same, so `sbx rm` removes the one entry.

## herdr and the pane layout

herdr on the Mac keeps "saved machines": one window, a sidebar with Local and
each SSH machine. A sandbox is an SSH config alias, so `sbx new` runs
`herdr machine add sbx-<name>`, and `sbx rm` removes the machine. herdr starts
its server side itself; the template holds the binary.

`sbxlib/layout.py` builds `.sandbox/herdr.toml` through
`herdr --machine sbx-<name>`, with no window open: `tab create`, then one
`pane split` per pane, then `pane rename` and `pane run`. Two facts shape it.
`--ratio` on a split is the share that the ORIGINAL pane keeps, so each ratio
is computed from what remains of the row or the column. And a remote pane's
`--cwd` is taken as given, so the CLI passes absolute paths. After the build it
reads `pane layout`, which reports each pane's rectangle, and warns when a
pane is not where the grid said. The herdr-spreader plugin cannot make such a
grid: it splits each pane from the one made just before it.

A pane's shell starts inside the project with no `cd`, so rvm's and nvm's `cd`
hooks have not chosen its Ruby, gemset and Node. The template's `~/.zshrc`
therefore ends with one `cd .`.

## Claude Code

**The subscription token.** `sbx claude-token` runs `claude setup-token`,
which prints a token valid for one year. The keychain holds the one master
copy (`sbx-claude-token`), and `~/.config/sbx/claude-token.toml` records its
date. `sbx new` writes `export CLAUDE_CODE_OAUTH_TOKEN=...` into
`~/.config/sbx/claude.env` in the sandbox, and `~/.zshenv` reads that file in
every shell. The token does not refresh itself. A copy of the Mac's own
Claude login would be no better: Claude Code replaces the refresh token when it
uses it, so copies on several machines sign each other out.

**Remote Control** needs a full claude.ai login. It refuses the
inference-only token. So each personal sandbox signs in once: the CLI starts
`claude auth login` in a tmux session in the sandbox, opens the URL on the
Mac, and types the pasted code into the sandbox. The server runs as a systemd
user service in tmux, logs to `~/.local/state/sbx/remote-control.log`, and
needs no inbound port. The CLI first writes the answers to two one-time
dialogs into `~/.claude.json`: the workspace trust and the Remote Control
consent. A full login carries `org:create_api_key` among its scopes, which is
why an agent sandbox never gets one: every entry point checks the profile.

## The template

**Versions.** The managers own them, and the template caches them. rvm reads
`.ruby-version`, nvm reads `.nvmrc`, and Go downloads the toolchain that
`go.mod` names, so a project always gets its own versions. `sbx versions`
scans the registered projects for those files and for the images in their
compose files, and `--write` puts the union into `host/local.conf`. The first
Ruby and Node in each list become the template's defaults.

**What is in it.** Ubuntu 24.04, and on top of it:

- build tools, shells (zsh, tmux), and command-line tools (jq, ripgrep, fd,
  fzf, direnv, gh, ffmpeg);
- libraries for Ruby gems: OpenSSL, readline, zlib, YAML, ffi, gmp, libpq,
  sqlite3, libvips, ImageMagick, and the Postgres and Redis clients;
- Docker with compose, Caddy, the Mesa software Vulkan driver;
- Node through nvm, Ruby through rvm, Go from the official tarball, and the
  cached Docker images;
- Chrome through `agent-browser`, with Playwright's system libraries;
- Claude Code and herdr;
- the extras that `SBX_TEMPLATE_EXTRAS` names (`template/extras/*.sh`);
- `sbx-mirror`, the DHCP-hostname unit, `sbx-recipe-run`, and a `~/.zshenv`
  that loads `rvm`, `nvm` and the PATH for every shell, including the
  non-interactive one of `ssh host command` and an agent's tool call.

The user `dev` has zsh, sudo with no password, `~/code`, and lingering on,
because herdr and an agent run for hours with no login session.

**How it is built.** `host/30-template-build.sh` makes the VM from the cloud
image (q35 and OVMF, so that a clone can take a PCIe GPU later). It puts the
provision script and its files into a cloud-init snippet as a base64 payload,
and starts the VM on the agent bridge. It proves within three minutes that
DHCP and DNS reached the VM, from the gateway's lease file. Then it watches the
provision through the guest agent: each new log line, a failure marker, a
warning after 15 quiet minutes, a stop after 45. On success the VM seals
itself (it deletes cloud-init's state, the machine ID, the SSH host keys and
the payload) and powers off. The host script then removes the snippet, turns
off the first-boot upgrade, converts the VM to a template, and gives the token
its access back. `--finish` attaches to a build VM that is already up.
`host/vm-diag.sh` shows what a build VM is doing, through the guest agent,
because the host has no route to a sandbox subnet.

**An extra** is a shell file in `template/extras/`, sourced by `provision.sh`
after the core packages, with its helpers and its error trap. It adds its
tools to `CHECK_TOOLS`, and the final check proves that they are on the user's
PATH in a non-interactive shell.

## The GPU

Three levels exist. The lowest level that does the job is the right one:

1. **Software Vulkan**, the default. Enough for a headless run of a wgpu
   application; too slow for a frame-time measurement.
2. **Shared Vulkan through virtio-gpu (Venus).** Experimental. Many sandboxes
   share the card, with no hardware encoder. It needs QEMU arguments that only
   root can set, so they belong in the template.
3. **Passthrough**, one sandbox at a time. A game stream needs this. The host
   console is dark while a VM holds the only GPU. Some cards do not reset after
   a VM stops; the `vendor-reset` module corrects that for many AMD cards. A
   token cannot name a raw PCI address, so root defines a PCI Resource Mapping
   once, and the token gets `Mapping.Use` on it.

Nothing at level 2 or 3 is scripted or tested.

## Traps

Each of these cost real time. Keep them in mind when you change the code.

| Trap | What happens | What to do |
|---|---|---|
| `*` as a Tailscale ACL source | includes approved subnet routes; every sandbox reaches every tailnet device | `autogroup:member` as the source; the nftables drop in a separate table |
| `WantedBy=multi-user.target` with `After=cloud-init.target` | an ordering cycle; systemd deletes the unit's job silently | `WantedBy=cloud-init.target`, `After=cloud-final.service` |
| a VM's first DHCP request | carries the image's host name, `ubuntu` | ask again after cloud-init with `networkctl reconfigure`, not `renew` |
| the Mac's negative DNS cache | a name looked up too early stays unknown ~75 s | ask dnsmasq directly first |
| `HostKeyAlias %h` in ssh_config | `%h` is not expanded there; every sandbox shares one alias | no `HostKeyAlias`; key by the resolved name |
| a `Host sbx-*` block and a full name | the block matches the full name and appends the domain again | the CLI uses `-F /dev/null` |
| `set -e` and `trap ERR` inside a function | the trap does not run without `set -E` | `set -Eeuo pipefail` and an `EXIT` trap |
| GNU `install -d -o user a/b` | the missing parent `a` is made as root | name every level |
| `/run` | mounted `noexec`; systemd cannot start a script there | put the script under `/usr/local/lib` |
| `sysctl --system` in an unprivileged container | exits non-zero on keys it may not write | `sysctl -p <own file>`, and read the value back |
| dnsmasq `expand-hosts` in a container | reads `/etc/hosts`, where Proxmox writes `127.0.1.1 <hostname>` | `no-hosts` |
| rvm and a partial version | `rvm install 3.4` asks for a tarball that does not exist; rvm stable's database ends in 2021 | full versions |
| rvm and `set -u` | rvm's functions and its `cd` hook read unset variables | never `set -u` in a script that loads rvm |
| rvm and `ssh host command` | rvm runs `exec 6>&2`; a daemon started there holds the session's stderr, and ssh never returns | `~/.zshenv` closes fd 6 in a non-interactive shell |
| `qm destroy` | removes every ACL on the destroyed VM's path | apply the token's ACLs again after a template rebuild |
| Ubuntu 24.04 and Chromium | AppArmor blocks unprivileged user namespaces: "No usable sandbox" | `kernel.apparmor_restrict_unprivileged_userns=0` |
| one machine ID across clones | one DHCP client ID, one address for every clone | an empty `/etc/machine-id` in the seal |
| a veth named `eth0` made in a container | collides with the container's own `eth0` | throwaway names, renamed on the move into a namespace |
| a probe harness written for bash, run by zsh | zsh does not word-split `$VAR`; every probe runs nothing | run it with bash; keep a control that must pass |
| `$HOME` in a systemd `ExecStart` | not expanded | the `%h` specifier |
| `script -c` and a zsh user | `script` runs the command through `$SHELL`, and zsh reads `~/.zshenv` again | `SHELL=/bin/sh` for `script` |
| `herdr machine add` from a tool | with a terminal on stdin and output in a pipe, herdr may ask a question nobody sees | run it with stdin closed; never let it fail the sandbox |
| a forwarded SSH agent and a key named in ssh config | the sandbox sees the agent only | `ssh-add --apple-use-keychain ~/.ssh/<key>`; the CLI checks before a VM exists |
| a Python f-string with `\"` inside `{}` | a syntax error before Python 3.12 | different quotes, or a heredoc |

## What is tested, and how

**Unit tests** (`python3 -m unittest discover -s tests -t .`, no
dependencies):

- the manifest parser, the path guard, the symbolic link refusal, the profile
  policy, and the bindings file;
- the order of API calls in `sbx new`; that a secret never appears in a command
  line or an API call; that the project's git token beats the global one; the
  late-name path; the herdr add and remove; the pane layout arithmetic;
- the API transport against a real local HTTPS server: the token header, the
  double encoding of `sshkeys`, the task wait, and that a wrong CA or a wrong
  pin stops the request before the token is sent;
- `sbx git-token` and `sbx claude-token`: the token reaches its consumer on
  stdin only, and a bad token stores nothing;
- `sbx setup`: the values proposed for a new host, an existing host that keeps
  its values, the order of the host steps, `--mac-only`, and the per-Mac token;
- `sbx doctor`: the token scope;
- the Rails example recipe against three kinds of application, with stubbed
  tools (`tests/test_rails_example.py`);
- the settings: the Python defaults equal `defaults.conf`, and
  `docs/reference.md` names every command and every key
  (`tests/test_docs.py`).

**Behaviour tests** (Docker):

- `tests/run-guard-test.sh`: the gateway rules in network namespaces, with a
  control run for every refusal and a stand-in for Tailscale's accept rule.
  Run it after each change to `gw/nftables.conf.tmpl`.
- `tests/run-dns-test.sh`: the name mechanism with a real DHCP client. Run it
  after each change to `gw/dnsmasq.conf.tmpl`.
- `tests/run-mirror-test.sh`: the port mirror with a real Caddy.
- `tests/run-finish-test.sh`: `30-template-build.sh --finish` with a fake `qm`
  and the real seal script.

**On the reference host,** by hand: the gateway, the template build, the
token, `sbx new` in 24 seconds, the first-boot naming from the boot journal,
the isolation table of [security.md](security.md), the mirror from a real
client, the herdr sidebar, and Remote Control in a personal sandbox. Every
refusal was checked with a controlled pair: the same probe that must fail in
one profile must pass in the other. A refusal with no control proves nothing,
because a broken network refuses everything. `sbx doctor --isolation` now
repeats that pair on any setup.

**Not tested automatically**, because each one needs a real host:

- `template/provision.sh` and the extras. A download URL or a package name can
  change.
- That Tailscale routes a subnet from an unprivileged container on your host.
- `sbx setup` against a real host (the unit tests use a fake host).
- GPU passthrough.

## The repository

```
bin/sbx                     the entry point (Python 3.11+, standard library only)
sbxlib/
  cli.py                    every command, the guide, the connect flow
  config.py                 the three settings layers
  hostsetup.py              sbx setup
  doctor.py                 sbx doctor
  pve.py                    the Proxmox HTTP API with the scoped token; tags
  vm.py                     ssh into a sandbox; resolves(); dns_has()
  manifest.py               the untrusted manifest parser and its guards
  inputs.py                 a decision for each input; the bindings file
  gittoken.py               sbx git-token
  claudetoken.py            sbx claude-token
  remotecontrol.py          Remote Control in a personal sandbox
  projects.py               the project registry; the project tag
  versions.py               sbx versions
  herdr.py                  saved machines
  layout.py                 a project's .sandbox/herdr.toml panes
  names.py                  the host name rules
  run.py                    one place that starts processes, replaceable in tests
host/
  defaults.conf             the shared settings; local.conf.example
  lib.sh                    the loader, render(), confirm()
  discover.sh               prints what sbx setup needs to know about the host
  10-bridges.sh             the two bridges
  20-gw-create.sh           the gateway container, its config, Tailscale
  30-template-build.sh      the template: build, --replace, --finish
  40-api-token.sh           the scoped token: --rotate, --acl-only, --emit, --token-id
  vm-diag.sh                what a build VM is doing, through the guest agent
gw/
  dnsmasq.conf.tmpl         DHCP and DNS
  nftables.conf.tmpl        the guard tables and NAT
  setup.sh                  runs inside the container
template/
  provision.sh              everything installed, once, on the first boot
  extras/                   the optional parts
  seal.sh                   what must differ between clones is removed
  files/                    sbx_mirror.py and its unit, the DHCP-hostname unit,
                            sbx-recipe-run, zshenv, zshrc, Caddyfile
tailscale/policy.example.hujson
examples/rails/.sandbox/    a Rails recipe, manifest and pane layout
tests/                      unit tests and four Docker behaviour tests
docs/                       the documentation
```
