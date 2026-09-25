#!/usr/bin/env bash
# Creates (or refreshes) the gateway container: DHCP + DNS for the sandbox
# subnets, NAT to the internet, the isolation rules, and the Tailscale subnet
# route. Run host/10-bridges.sh first.
#
#   20-gw-create.sh            create the container, or refresh its config
#   20-gw-create.sh --tailscale   run `tailscale up` (prints a login URL)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_pve

CT="$SBX_GW_CTID"
ROUTES="${SBX_AGENT_NET}.0/24,${SBX_PERSONAL_NET}.0/24"

tailscale_up() {
  log "starting Tailscale; open the URL that it prints"
  echo "The tailnet policy must already name $SBX_TAILSCALE_TAG in tagOwners,"
  echo "or this command is refused. See tailscale/policy.example.hujson."
  # --accept-dns=false: MagicDNS must not rewrite the gateway's resolver, or
  # the split-DNS rule for $SBX_DOMAIN would point dnsmasq at itself.
  pct exec "$CT" -- tailscale up \
    --hostname="$SBX_GW_HOSTNAME" \
    --advertise-routes="$ROUTES" \
    --advertise-tags="$SBX_TAILSCALE_TAG" \
    --accept-dns=false
  pct exec "$CT" -- tailscale status | head -5
}

if [[ "${1:-}" == "--tailscale" ]]; then
  tailscale_up
  exit 0
fi

for bridge in "$SBX_AGENT_BRIDGE" "$SBX_PERSONAL_BRIDGE"; do
  ip link show "$bridge" >/dev/null 2>&1 || die "$bridge is missing; run 10-bridges.sh first"
done

if ! pct status "$CT" >/dev/null 2>&1; then
  log "finding a Debian container template"
  pveam update >/dev/null
  tmpl="$(pveam available --section system | awk '{print $2}' \
          | grep -E '^debian-1[23]-standard_.*amd64' | sort -V | tail -1)"
  [[ -n "$tmpl" ]] || die "no debian-12/13 standard template in 'pveam available'"
  pveam list "$SBX_GW_TEMPLATE_STORAGE" | grep -q "$tmpl" \
    || pveam download "$SBX_GW_TEMPLATE_STORAGE" "$tmpl"

  lan="ip=dhcp"
  if [[ "$SBX_GW_LAN_IP" != "dhcp" ]]; then
    [[ -n "$SBX_GW_LAN_GW" ]] || die "SBX_GW_LAN_IP is static, so SBX_GW_LAN_GW must name the router"
    lan="ip=$SBX_GW_LAN_IP,gw=$SBX_GW_LAN_GW"
  fi

  log "creating container $CT ($SBX_GW_HOSTNAME)"
  pct create "$CT" "$SBX_GW_TEMPLATE_STORAGE:vztmpl/$tmpl" \
    --hostname "$SBX_GW_HOSTNAME" \
    --unprivileged 1 \
    --features nesting=1 \
    --cores 1 --memory 512 --swap 0 \
    --rootfs "$SBX_GW_STORAGE:4" \
    --net0 "name=lan0,bridge=$SBX_LAN_BRIDGE,$lan" \
    --net1 "name=sbxa0,bridge=$SBX_AGENT_BRIDGE,ip=${SBX_AGENT_NET}.1/24" \
    --net2 "name=sbxp0,bridge=$SBX_PERSONAL_BRIDGE,ip=${SBX_PERSONAL_NET}.1/24" \
    --onboot 1 \
    --description "sbx gateway: DHCP+DNS for $SBX_DOMAIN, NAT, isolation rules, Tailscale subnet route"

  # Tailscale routes a subnet only with a kernel TUN device. An unprivileged
  # container gets none unless the device is allowed and bind-mounted.
  cat >> "/etc/pve/lxc/$CT.conf" <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF
else
  log "container $CT exists; refreshing its config"
fi

pct status "$CT" | grep -q running || pct start "$CT"
# Wait for the LAN leg: setup.sh needs the internet for apt and Tailscale.
for _ in $(seq 1 30); do
  pct exec "$CT" -- sh -c 'ip -4 route show default | grep -q lan0' && break
  sleep 1
done
pct exec "$CT" -- sh -c 'ip -4 route show default | grep -q lan0' \
  || die "the container got no default route on lan0. If $SBX_LAN_BRIDGE has no DHCP server, set SBX_GW_LAN_IP and SBX_GW_LAN_GW in host/local.conf, destroy container $CT, and run this script again"

log "rendering the gateway config"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
SBX_DNSMASQ_SERVERS="$(for s in $SBX_UPSTREAM_DNS; do echo "server=$s"; done)"
export SBX_DNSMASQ_SERVERS
render "$SBX_ROOT_DIR/gw/dnsmasq.conf.tmpl"  > "$stage/dnsmasq.conf"
render "$SBX_ROOT_DIR/gw/nftables.conf.tmpl" > "$stage/nftables.conf"
cp "$SBX_ROOT_DIR/gw/setup.sh" "$stage/setup.sh"

pct exec "$CT" -- mkdir -p /root/gw
for f in dnsmasq.conf nftables.conf setup.sh; do
  pct push "$CT" "$stage/$f" "/root/gw/$f"
done
pct exec "$CT" -- bash /root/gw/setup.sh

if pct exec "$CT" -- tailscale status >/dev/null 2>&1; then
  log "Tailscale is already logged in"
else
  echo
  echo "Next: bash $0 --tailscale"
  echo "Then, in the Tailscale admin console:"
  echo "  1. approve the routes $ROUTES (or use autoApprovers in the policy)"
  echo "  2. DNS > Nameservers > Add > Custom: ${SBX_AGENT_NET}.1,"
  echo "     restricted to the domain $SBX_DOMAIN (split DNS)"
fi
log "done"
