#!/usr/bin/env bash
# Runs INSIDE the gateway container. host/20-gw-create.sh pushes this directory
# to /root/gw with the two .conf files already rendered, then runs this script.
# Idempotent: a second run refreshes the config and restarts the services.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

export DEBIAN_FRONTEND=noninteractive
# SSH passes the caller's LANG in, and the container has no such locale; every
# package script then prints a page of warnings. C.UTF-8 always exists.
export LANG=C.UTF-8 LC_ALL=C.UTF-8
apt-get update -q
apt-get install -y -q --no-install-recommends \
  dnsmasq nftables curl ca-certificates unattended-upgrades iproute2 dnsutils

# Route between the legs. IPv6 forwarding stays off: the sandbox subnets have
# no IPv6, and an open v6 forward path would bypass every v4 rule.
cat > /etc/sysctl.d/90-sbx-gw.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 0
EOF
# ONLY this file, never `sysctl --system`: an unprivileged container may not
# write the kernel.* and fs.* keys that the distribution's own files set, so
# --system exits non-zero even though these two keys applied. At boot,
# systemd-sysctl reads the same file and skips what it may not write.
sysctl -q -p /etc/sysctl.d/90-sbx-gw.conf
[[ "$(cat /proc/sys/net/ipv4/ip_forward)" == "1" ]] || {
  echo "ERROR: net.ipv4.ip_forward is not 1; the gateway cannot route" >&2
  exit 1
}

install -m 0644 dnsmasq.conf /etc/dnsmasq.d/sbx.conf
install -m 0755 nftables.conf /etc/nftables.conf

# Check both files before a restart can take the gateway down.
nft -c -f /etc/nftables.conf
dnsmasq --test --conf-file=/etc/dnsmasq.d/sbx.conf

systemctl enable -q nftables dnsmasq
systemctl restart nftables
systemctl restart dnsmasq

# The gateway is the security boundary and dnsmasq listens to the sandboxes,
# so security updates install themselves.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable -q --now tailscaled

[[ -c /dev/net/tun ]] || {
  echo "ERROR: /dev/net/tun is missing; Tailscale cannot route a subnet without it" >&2
  exit 1
}

# A state report, so one paste shows whether each part really runs.
echo
echo "=== gateway state ==="
for unit in dnsmasq nftables tailscaled; do printf '%-12s %s\n' "$unit" "$(systemctl is-active "$unit")"; done
printf '%-12s %s\n' "ip_forward" "$(cat /proc/sys/net/ipv4/ip_forward)"
printf '%-12s %s\n' "guard rules" "$(nft list chain inet sbx_guard forward 2>/dev/null | grep -c drop) drop rules in the forward chain"
printf '%-12s %s\n' "dhcp socket" "$(ss -Hlun 'sport = :67' | wc -l) listener(s) on udp/67"
printf '%-12s %s\n' "dns socket" "$(ss -Hlun 'sport = :53' | wc -l) listener(s) on udp/53"
ip -br -4 addr show | grep -E 'lan0|sbxa0|sbxp0' || true
echo "gateway services are up"
