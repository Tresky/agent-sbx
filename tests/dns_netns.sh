#!/usr/bin/env bash
# Behaviour test for gw/dnsmasq.conf.tmpl: the ONE mechanism that names a
# sandbox. Runs INSIDE a privileged Debian 13 container (tests/run-dns-test.sh).
# A real DHCP client in a network namespace asks for an address with a hostname;
# the test then asks dnsmasq for that name.
set -u
CONF="$1"; DOMAIN="$2"
fails=0
check() { # check <label> <got> <want>
  if [[ "$2" == "$3" ]]; then printf '  ok    %-56s %s\n' "$1" "${2:-<nothing>}"
  else printf '  WRONG %-56s got "%s", want "%s"\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}
ask() { dig +short +time=2 +tries=1 "$1" @10.77.0.1 | tr '\n' ' ' | sed 's/ $//'; }
client() { # client <netns> <hostname>
  ip netns add "$1"
  ip link add "h$1" type veth peer name "c$1"
  ip link set "h$1" master sbxa0 up
  ip link set "c$1" netns "$1" name eth0
  ip netns exec "$1" ip link set lo up; ip netns exec "$1" ip link set eth0 up
  ip netns exec "$1" udhcpc -i eth0 -x "hostname:$2" -n -q -t 5 -s /bin/true 2>&1 | grep -o 'lease of [0-9.]*' | awk '{print $3}'
}

# sbxa0 is a bridge here, so several clients can sit on the agent subnet. On
# the real gateway it is the container's leg on vmbr77.
ip link add sbxa0 type bridge; ip link set sbxa0 up; ip addr add 10.77.0.1/24 dev sbxa0
printf '127.0.0.1 localhost\n127.0.1.1 sbx-gw\n' > /etc/hosts 2>/dev/null || true

dnsmasq --conf-file="$CONF" --pid-file=/tmp/d.pid --dhcp-leasefile=/tmp/leases || { echo "dnsmasq did not start"; exit 2; }
sleep 1

echo "== before any client (control)"
check "sbx-myapp.$DOMAIN"                       "$(ask "sbx-myapp.$DOMAIN")" ""
echo "== a client takes a lease with hostname sbx-myapp"
ip1="$(client a sbx-myapp)"
check "the client got a lease in the agent range" "$([[ "$ip1" == 10.77.0.* ]] && echo yes)" "yes"
check "sbx-myapp.$DOMAIN -> the lease address"  "$(ask "sbx-myapp.$DOMAIN")" "$ip1"
check "the short name resolves too"               "$(ask sbx-myapp)" "$ip1"
check "the gateway's name has ONE address"        "$(ask "sbx-gw.$DOMAIN")" "10.77.0.1"
check "an unknown name in the zone is not forwarded" "$(ask "nope.$DOMAIN")" ""
echo "== a second client asks for the SAME name (a rebuilt sandbox)"
ip2="$(client b sbx-myapp)"
check "the name moves to the newest lease"        "$(ask "sbx-myapp.$DOMAIN")" "$ip2"
check "the two leases differ"                     "$([[ -n "$ip2" && "$ip1" != "$ip2" ]] && echo yes)" "yes"
echo "== a client first asks as 'ubuntu', then again as sbx-lab (a clone after cloud-init)"
# The real gateway showed that a VM's first request carries the image's own
# name; sbx-dhcp-hostname.service asks again once cloud-init has set the name.
ip3="$(client c ubuntu)"
check "ubuntu.$DOMAIN exists after the first request" "$(ask "ubuntu.$DOMAIN")" "$ip3"
ip4="$(ip netns exec c udhcpc -i eth0 -x hostname:sbx-lab -n -q -t 5 -s /bin/true 2>&1 | grep -o 'lease of [0-9.]*' | awk '{print $3}')"
check "the client keeps its address"              "$ip4" "$ip3"
check "sbx-lab.$DOMAIN -> that address"           "$(ask "sbx-lab.$DOMAIN")" "$ip3"
check "the old name 'ubuntu' is gone"             "$(ask "ubuntu.$DOMAIN")" ""

kill "$(cat /tmp/d.pid)" 2>/dev/null
echo
if [[ $fails -eq 0 ]]; then echo "DNS TEST PASSED"; else echo "DNS TEST FAILED: $fails wrong result(s)"; fi
exit "$fails"
