#!/usr/bin/env bash
# Behaviour test for the sidecar design: sidecar/nftables.conf.tmpl,
# sidecar/sidecar.py, and the VLAN-per-sandbox wiring, against the real
# gateway rules. Runs INSIDE a privileged Debian container
# (tests/run-sidecar-test.sh starts it). The container's own namespace plays
# the Proxmox host: it holds the VLAN-aware bridge and no address on it.
#
#   agentA 10.79.0.2 ==VLAN 9150== agent0 sideA net0 10.77.0.57 --+
#   agentB 10.79.0.6 ==VLAN 9151== agent0 sideB net0 10.77.0.58 --+-- vmbr77 (untagged) -- sbxa0 gw
#                                                                          gw lan0 ------- lan 192.168.50.10 (+ 203.0.113.10 = "internet",
#                                                                                                             with a fake API on :9000)
#                                                                          gw tailscale0 -- ts  100.64.0.2   (your Mac, on the tailnet)
#
# Every refusal is a CONTROLLED PAIR: the same probe runs again with the rule
# that refused it removed, and must pass then. A probe that fails both ways
# proves nothing about the design; it means the topology is broken.
set -u
GW_RULES="$1"
SIDECAR_RULES="$2"
fails=0

ns() { ip netns exec "$@"; }

# port <bridge-side name> <ns> <ifname in ns> [vlan]
# One veth end stays here on the bridge, like a VM's tap on the host. With a
# vlan, the port carries that VLAN alone, untagged toward the guest: exactly
# what Proxmox does for `net0: ...,bridge=vmbr77,tag=<vlan>`.
port() {
  ip link add "$1" type veth peer name "p$1" || exit 2
  ip link set "p$1" netns "$2" name "$3" || exit 2
  ip link set "$1" master vmbr77 up
  ns "$2" ip link set "$3" up
  if [[ -n "${4:-}" ]]; then
    bridge vlan del dev "$1" vid 1
    bridge vlan add dev "$1" vid "$4" pvid untagged
  fi
}
pairs=0
link() { # link <nsA> <ifA> <nsB> <ifB>: a plain wire between two namespaces
  pairs=$((pairs + 1))
  ip link add "va$pairs" type veth peer name "vb$pairs" || exit 2
  ip link set "va$pairs" netns "$1" name "$2" || exit 2
  ip link set "vb$pairs" netns "$3" name "$4" || exit 2
  ns "$1" ip link set "$2" up; ns "$3" ip link set "$4" up
}

for n in gw agentA agentB sideA sideB lan ts; do ip netns add "$n"; ns "$n" ip link set lo up; done

# The host: one VLAN-aware bridge, no address on it.
ip link add vmbr77 type bridge vlan_filtering 1
ip link set vmbr77 up
port tapA agentA eth0   9150
port sA0  sideA  agent0 9150
port sA1  sideA  net0
port tapB agentB eth0   9151
port sB0  sideB  agent0 9151
port sB1  sideB  net0
port gwp  gw     sbxa0
link lan eth0 gw lan0
link ts  eth0 gw tailscale0

ns gw ip addr add 10.77.0.1/24 dev sbxa0
ns gw ip addr add 192.168.50.2/24 dev lan0;      ns lan ip addr add 192.168.50.10/24 dev eth0
ns gw ip addr add 100.64.0.1/10 dev tailscale0;  ns ts  ip addr add 100.64.0.2/10 dev eth0
ns lan ip addr add 203.0.113.10/32 dev lo
ns gw ip route add default via 192.168.50.10
ns ts ip route add 10.77.0.0/24 via 100.64.0.1
# The LAN host has NO route to the sandbox subnets on a real LAN, except for
# the probes that test the lan0 -> sidecar rule.
ns lan ip route add 10.77.0.0/24 via 192.168.50.2
ns gw sysctl -qw net.ipv4.ip_forward=1

# Each pair: a /30 on its own VLAN. (The real design can give every pair the
# same /30, since no two pairs share a segment; the test uses two, so that a
# probe can name the other VM.)
ns sideA ip addr add 10.79.0.1/30 dev agent0;  ns agentA ip addr add 10.79.0.2/30 dev eth0
ns sideB ip addr add 10.79.0.5/30 dev agent0;  ns agentB ip addr add 10.79.0.6/30 dev eth0
ns sideA ip addr add 10.77.0.57/24 dev net0;   ns sideB ip addr add 10.77.0.58/24 dev net0
ns sideA ip route add default via 10.77.0.1;   ns sideB ip route add default via 10.77.0.1
ns agentA ip route add default via 10.79.0.1;  ns agentB ip route add default via 10.79.0.5
ns sideA sysctl -qw net.ipv4.ip_forward=1;     ns sideB sysctl -qw net.ipv4.ip_forward=1
# A hostile agent may try the other VM DIRECTLY on the bridge, not through its
# sidecar. These routes make that attempt; only the VLAN can stop it.
ns agentA ip route add 10.79.0.4/30 dev eth0
ns agentB ip route add 10.79.0.0/30 dev eth0

# The gateway: the real rules, plus a stand-in for Tailscale's accept toward
# tailscale0, which the guard must beat (as in guard_netns.sh).
ns gw nft -f - <<'EOF'
table inet fake_tailscale {
	chain forward { type filter hook forward priority 0; policy accept; oifname "tailscale0" accept; }
}
EOF
ns gw nft -f "$GW_RULES" || { echo "gateway rules failed to load"; exit 2; }
ns sideA nft -f "$SIDECAR_RULES" || { echo "sidecar rules failed to load"; exit 2; }
ns sideB nft -f "$SIDECAR_RULES" || { echo "sidecar rules failed to load"; exit 2; }

# Services. Port 8000 everywhere is the generic probe target; the fake API
# answers on the "internet"; 4400 in agent A is the dev server to expose.
SA="placeholder-for-sandbox-A"; SB="placeholder-for-sandbox-B"
printf '%s\n' "$SA" > /tmp/secretA; printf '%s\n' "$SB" > /tmp/secretB
printf 'claude=REAL-CLAUDE-A\ngithub=REAL-GITHUB-A\n' > /tmp/tokensA
printf 'claude=REAL-CLAUDE-B\ngithub=REAL-GITHUB-B\n' > /tmp/tokensB
for n in lan ts gw agentA agentB; do ns "$n" python3 -m http.server 8000 --bind 0.0.0.0 >/dev/null 2>&1 & done
ns agentA python3 -m http.server 4400 --bind 0.0.0.0 >/dev/null 2>&1 &
ns lan python3 /sbx/tests/sidecar_fake_api.py 203.0.113.10 9000 &
ns sideA python3 /sbx/sidecar/sidecar.py --agent-addr 10.79.0.1 --vm-addr 10.79.0.2 --net-addr 10.77.0.57 \
  --secret-file /tmp/secretA --tokens-file /tmp/tokensA \
  --claude-upstream http://203.0.113.10:9000 --github-upstream http://203.0.113.10:9000 2>/tmp/sideA.log &
ns sideB python3 /sbx/sidecar/sidecar.py --agent-addr 10.79.0.5 --vm-addr 10.79.0.6 --net-addr 10.77.0.58 \
  --secret-file /tmp/secretB --tokens-file /tmp/tokensB \
  --claude-upstream http://203.0.113.10:9000 --github-upstream http://203.0.113.10:9000 2>/tmp/sideB.log &
sleep 2

report() { # report <label> <got> <want>
  if [[ "$2" == "$3" ]]; then printf '  ok    %-56s %s\n' "$1" "$2"
  else printf '  WRONG %-56s got %s, want %s\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}
probe() { ns "$1" ping -c1 -W1 "$2" >/dev/null 2>&1 && ns "$1" timeout 2 bash -c "exec 3<>/dev/tcp/$2/8000" 2>/dev/null; }
probe_ping() { ns "$1" ping -c1 -W1 "$2" >/dev/null 2>&1; }
tcp() { ns "$1" timeout 2 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null; }
# expect <pass|fail> <label> <ns> <target> [ping]
expect() {
  local got=pass
  if [[ "${5:-}" == ping ]]; then probe_ping "$3" "$4" || got=fail; else probe "$3" "$4" || got=fail; fi
  report "$2" "$got" "$1"
}
# expect_tcp <pass|fail> <label> <ns> <addr> <port>
expect_tcp() { local got=pass; tcp "$3" "$4" "$5" || got=fail; report "$2" "$got" "$1"; }
# contains <label> <text> <needle>
contains() { if grep -q -- "$3" <<<"$2"; then report "$1" "found" "found"; else report "$1" "missing: ${2:0:80}" "found"; fi; }
c() { ns "$1" curl -s -m 3 "${@:2}"; }
code() { ns "$1" curl -s -m 3 -o /dev/null -w '%{http_code}' "${@:2}"; }

echo "== the wire: what an agent reaches through, and around, its sidecar"
expect     pass "agent A -> its sidecar (ping)"                      agentA 10.79.0.1 ping
expect     pass "agent A -> internet, through sidecar and gateway"   agentA 203.0.113.10
expect     fail "agent A -> LAN host"                                agentA 192.168.50.10
expect     fail "agent A -> tailnet device"                          agentA 100.64.0.2
expect     fail "agent A -> gateway (ping)"                          agentA 10.77.0.1 ping
expect_tcp fail "agent A -> gateway TCP service"                     agentA 10.77.0.1 8000
expect     fail "agent A -> sidecar B (ping)"                        agentA 10.77.0.58 ping
expect     fail "agent A -> agent B, directly on the bridge"         agentA 10.79.0.6 ping
expect_tcp fail "sidecar A -> sidecar B expose API"                  sideA  10.77.0.58 8081
expect     fail "LAN host -> sidecar A (ping)"                       lan    10.77.0.57 ping
expect     pass "tailnet device -> sidecar A (ping)"                 ts     10.77.0.57 ping
expect_tcp pass "tailnet device -> sidecar A expose API"             ts     10.77.0.57 8081

echo "== the credential proxy: the placeholder never leaves, the real token never enters"
out="$(c agentA -H "Authorization: Bearer $SA" http://10.79.0.1:8080/v1/messages)"
contains "Claude call carries the real token upstream"               "$out" "Bearer REAL-CLAUDE-A"
out="$(c agentA -H "Authorization: Bearer $SA" http://10.79.0.1:8080/github/repos/o/r)"
contains "git call carries the git token upstream"                   "$out" "Bearer REAL-GITHUB-A"
contains "git call keeps its path"                                   "$out" '"path": "/repos/o/r"'
report   "wrong placeholder is refused"        "$(code agentA -H 'Authorization: Bearer wrong' http://10.79.0.1:8080/v1/messages)" 401
report   "no credential is refused"            "$(code agentA http://10.79.0.1:8080/v1/messages)" 401
report   "A's placeholder is useless at sidecar B" "$(code agentB -H "Authorization: Bearer $SA" http://10.79.0.5:8080/v1/messages)" 401
out="$(c agentA -H "Authorization: Bearer $SA" http://203.0.113.10:9000/v1/messages)"
contains "the placeholder sent straight to the internet is only itself" "$out" "Bearer $SA"

echo "== the expose API: the sandbox asks, only the trusted side approves"
expect_tcp fail "tailnet device -> port 4400 before approval"        ts 10.77.0.57 4400
report "sandbox asks for 4400"                 "$(code agentA -H "Authorization: Bearer $SA" -d '{"port":4400}' http://10.79.0.1:8081/expose)" 202
report "sandbox asks with no credential"       "$(code agentA -d '{"port":4400}' http://10.79.0.1:8081/expose)" 401
report "sandbox cannot approve its own request" "$(code agentA -H "Authorization: Bearer $SA" -d '{"port":4400}' http://10.79.0.1:8081/approve)" 403
contains "trusted side sees the request pending" "$(c ts http://10.77.0.57:8081/requests)" pending
report "trusted side approves"                 "$(code ts -d '{"port":4400}' http://10.77.0.57:8081/approve)" 200
report "tailnet device -> port 4400 after approval" "$(code ts http://10.77.0.57:4400/)" 200
expect_tcp fail "agent B -> sidecar A port 4400"                     agentB 10.77.0.57 4400
expect_tcp fail "LAN host -> sidecar A port 4400"                    lan    10.77.0.57 4400
report "trusted side denies"                   "$(code ts -d '{"port":4400}' http://10.77.0.57:8081/deny)" 200
expect_tcp fail "tailnet device -> port 4400 after denial"           ts 10.77.0.57 4400
report "a port outside the range is refused"   "$(code agentA -H "Authorization: Bearer $SA" -d '{"port":22}' http://10.79.0.1:8081/expose)" 400

echo "== control 1: sidecar rules deleted (the gateway's own layer still holds)"
for s in sideA sideB; do ns "$s" nft delete table inet sbx_sidecar; done
expect     pass "agent A -> gateway (ping)"                          agentA 10.77.0.1 ping
expect     pass "agent A -> sidecar B (ping)"                        agentA 10.77.0.58 ping
expect_tcp pass "sidecar A -> sidecar B expose API"                  sideA  10.77.0.58 8081
expect     fail "agent A -> LAN host (gateway drops it)"             agentA 192.168.50.10
expect     fail "agent A -> tailnet device (gateway drops it)"       agentA 100.64.0.2

echo "== control 2: agent B moved onto agent A's VLAN"
bridge vlan del dev tapB vid 9151
bridge vlan add dev tapB vid 9150 pvid untagged
expect     pass "agent A -> agent B, directly on the bridge"         agentA 10.79.0.6 ping

echo "== control 3: gateway guard deleted too (the topology itself is whole)"
ns gw nft delete table inet sbx_guard
expect     pass "agent A -> LAN host"                                agentA 192.168.50.10
expect     pass "agent A -> tailnet device"                          agentA 100.64.0.2
expect     pass "LAN host -> sidecar A (ping)"                       lan    10.77.0.57 ping

echo
echo "-- sidecar A log --"; cat /tmp/sideA.log
echo
if [[ $fails -eq 0 ]]; then echo "SIDECAR TEST PASSED"; else echo "SIDECAR TEST FAILED: $fails wrong result(s)"; fi
exit "$fails"
