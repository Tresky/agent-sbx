#!/usr/bin/env bash
# Behaviour test for gw/nftables.conf.tmpl. Runs INSIDE a privileged Debian
# container (tests/run-guard-test.sh starts it). Builds this topology from
# network namespaces and probes it:
#
#   agent 10.77.0.57 --sbxa0--+
#   pers  10.78.0.57 --sbxp0--+-- gw --lan0-------- lan 192.168.50.10 (+ 203.0.113.10 = "internet")
#                             +------tailscale0---- ts  100.64.0.2    (a tailnet device)
#
# Every refusal is a CONTROLLED PAIR: the same probe runs with the guard table
# loaded and with it deleted. A probe that fails in both runs proves nothing
# about the guard; it means the topology is broken.
#
# A stand-in for Tailscale's own rules is loaded too: a forward chain that
# ACCEPTS everything toward tailscale0. The guard must win against it.
set -u
RULES="$1"
fails=0

ns() { ip netns exec "$@"; }
pairs=0
link() { # link <nsA> <ifA> <nsB> <ifB>
  # Made under throwaway names and renamed on the move: a veth created as
  # "eth0" collides with the container's own eth0 before it leaves this netns.
  pairs=$((pairs + 1))
  ip link add "va$pairs" type veth peer name "vb$pairs" || exit 2
  ip link set "va$pairs" netns "$1" name "$2" || exit 2
  ip link set "vb$pairs" netns "$3" name "$4" || exit 2
  ns "$1" ip link set "$2" up; ns "$3" ip link set "$4" up
}

for n in gw agent pers lan ts; do ip netns add "$n"; ns "$n" ip link set lo up; done
link agent eth0 gw sbxa0;      link pers eth0 gw sbxp0
link lan   eth0 gw lan0;       link ts   eth0 gw tailscale0

ns gw ip addr add 10.77.0.1/24 dev sbxa0;        ns agent ip addr add 10.77.0.57/24 dev eth0
ns gw ip addr add 10.78.0.1/24 dev sbxp0;        ns pers  ip addr add 10.78.0.57/24 dev eth0
ns gw ip addr add 192.168.50.2/24 dev lan0;      ns lan   ip addr add 192.168.50.10/24 dev eth0
ns gw ip addr add 100.64.0.1/10 dev tailscale0;  ns ts    ip addr add 100.64.0.2/10 dev eth0
ns lan ip addr add 203.0.113.10/32 dev lo
ns agent ip route add default via 10.77.0.1;     ns pers ip route add default via 10.78.0.1
ns gw ip route add default via 192.168.50.10
ns ts ip route add 10.77.0.0/24 via 100.64.0.1;  ns ts ip route add 10.78.0.0/24 via 100.64.0.1
# The LAN host has NO route to the sandbox subnets, as on a real LAN, except
# for the one probe that tests the lan0 -> sandbox rule.
ns lan ip route add 10.77.0.0/24 via 192.168.50.2
ns gw sysctl -qw net.ipv4.ip_forward=1

ns gw nft -f - <<'EOF'
table inet fake_tailscale {
	chain forward { type filter hook forward priority 0; policy accept; oifname "tailscale0" accept; }
}
EOF

for n in agent lan ts; do ns "$n" python3 -m http.server 8000 --bind 0.0.0.0 >/dev/null 2>&1 & done
sleep 1

probe() { ns "$1" ping -c1 -W1 "$2" >/dev/null 2>&1 && ns "$1" timeout 2 bash -c "exec 3<>/dev/tcp/$2/8000" 2>/dev/null; }
probe_ping() { ns "$1" ping -c1 -W1 "$2" >/dev/null 2>&1; }

# expect <pass|fail> <label> <ns> <target> [ping]
expect() {
  local want="$1" label="$2" got=pass
  if [[ "${5:-}" == ping ]]; then probe_ping "$3" "$4" || got=fail; else probe "$3" "$4" || got=fail; fi
  if [[ "$got" == "$want" ]]; then printf '  ok    %-52s %s\n' "$label" "$got"
  else printf '  WRONG %-52s got %s, want %s\n' "$label" "$got" "$want"; fails=$((fails + 1)); fi
}

echo "== guard loaded"
ns gw nft -f "$RULES" || { echo "rules failed to load"; exit 2; }
expect fail "agent -> tailnet device"             agent 100.64.0.2
expect fail "agent -> LAN host"                   agent 192.168.50.10
expect fail "agent -> personal sandbox"           agent 10.78.0.57 ping
expect pass "agent -> internet"                   agent 203.0.113.10
expect pass "agent -> gateway (ping)"             agent 10.77.0.1 ping
expect fail "personal -> tailnet device"          pers  100.64.0.2
expect pass "personal -> LAN host"                pers  192.168.50.10
expect pass "personal -> internet"                pers  203.0.113.10
expect pass "tailnet device -> agent sandbox"     ts    10.77.0.57
expect fail "LAN host -> agent sandbox"           lan   10.77.0.57
# A sandbox may ask the gateway for DNS and DHCP and nothing else.
ns gw python3 -m http.server 8000 --bind 0.0.0.0 >/dev/null 2>&1 &
sleep 1
ns agent timeout 2 bash -c 'exec 3<>/dev/tcp/10.77.0.1/8000' 2>/dev/null \
  && { echo "  WRONG agent reached a TCP service on the gateway"; fails=$((fails + 1)); } \
  || echo "  ok    agent -> gateway TCP service                         fail"

echo "== guard deleted (the control run: every refusal above must now pass)"
ns gw nft delete table inet sbx_guard
expect pass "agent -> tailnet device"             agent 100.64.0.2
expect pass "agent -> LAN host"                   agent 192.168.50.10
expect pass "agent -> personal sandbox"           agent 10.78.0.57 ping
expect pass "personal -> tailnet device"          pers  100.64.0.2
expect pass "LAN host -> agent sandbox"           lan   10.77.0.57
ns agent timeout 2 bash -c 'exec 3<>/dev/tcp/10.77.0.1/8000' 2>/dev/null \
  && echo "  ok    agent -> gateway TCP service                         pass" \
  || { echo "  WRONG control: agent could not reach the gateway service"; fails=$((fails + 1)); }

echo
if [[ $fails -eq 0 ]]; then echo "GUARD TEST PASSED"; else echo "GUARD TEST FAILED: $fails wrong result(s)"; fi
exit "$fails"
