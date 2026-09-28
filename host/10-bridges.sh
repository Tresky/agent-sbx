#!/usr/bin/env bash
# Creates the two sandbox bridges on the Proxmox host. Idempotent.
#
# A sandbox bridge has no physical port and the host takes no address on it.
# The only way out of a sandbox subnet is the gateway container, which is where
# every firewall rule lives.
#
# The agent bridge is VLAN-aware: each agent sandbox and its sidecar share a
# VLAN of their own (the tag follows the sandbox's place in the id range), and the sidecars and the
# gateway sit untagged. The host's own interface on a bridge gets no IPv6
# link-local address either, so a sandbox cannot reach the hypervisor that way.
#
# A stanza from before sidecars lacks the VLAN lines; this script offers to add
# them, and reloads the network.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_pve

IFACES=/etc/network/interfaces
NO_IPV6='post-up sysctl -qw net.ipv6.conf.$IFACE.disable_ipv6=1'

stanza() { # stanza <bridge> <agent|personal>
  cat <<EOF

auto $1
iface $1 inet manual
	bridge-ports none
	bridge-stp off
	bridge-fd 0
EOF
  if [[ "$2" == agent ]]; then
    printf '\tbridge-vlan-aware yes\n\tbridge-vids 2-4094\n'
  fi
  printf '\t%s\n' "$NO_IPV6"
  echo "#sbx: $2 sandbox subnet. The gateway container $SBX_GW_CTID routes it."
}

# The lines that an existing stanza of <bridge> lacks, one per line.
missing_lines() { # missing_lines <bridge> <agent|personal>
  local block
  block="$(awk -v b="$1" '$1 == "iface" && $2 == b {p=1; next} p && /^(auto|iface|source)/ {p=0} p' "$IFACES")"
  if [[ "$2" == agent ]]; then
    grep -q 'bridge-vlan-aware yes' <<<"$block" || echo "bridge-vlan-aware yes"
    grep -q 'bridge-vids' <<<"$block" || echo "bridge-vids 2-4094"
  fi
  grep -qF 'disable_ipv6=1' <<<"$block" || echo "$NO_IPV6"
}

# Inserts lines after the `bridge-fd` line of <bridge>'s stanza.
add_lines() { # add_lines <bridge> <lines...>
  local bridge="$1"; shift
  python3 - "$IFACES" "$bridge" "$@" <<'EOF'
import sys
path, bridge, *lines = sys.argv[1:]
out, inside, done = [], False, False
for line in open(path).read().splitlines():
    word = line.split()
    if word[:2] == ["iface", bridge]:
        inside = True
    elif word and word[0] in ("auto", "iface", "source"):
        inside = False
    out.append(line)
    if inside and not done and word[:1] == ["bridge-fd"]:
        out += ["\t" + l for l in lines]
        done = True
if not done:
    sys.exit(f"no bridge-fd line in the stanza of {bridge}")
open(path, "w").write("\n".join(out) + "\n")
EOF
}

missing=()   # stanzas to append
upgrade=()   # "bridge|line" pairs to insert
for pair in "$SBX_AGENT_BRIDGE:agent" "$SBX_PERSONAL_BRIDGE:personal"; do
  bridge="${pair%%:*}"; kind="${pair##*:}"
  if grep -qE "^iface ${bridge} " "$IFACES"; then
    lines="$(missing_lines "$bridge" "$kind")"
    if [[ -z "$lines" ]]; then
      log "$bridge is already in $IFACES, with every line"
    else
      while IFS= read -r l; do upgrade+=("$bridge|$l"); done <<<"$lines"
    fi
  else
    missing+=("$pair")
  fi
done

if [[ ${#missing[@]} -eq 0 && ${#upgrade[@]} -eq 0 ]]; then
  log "nothing to do"
  exit 0
fi

echo "This script changes $IFACES and runs 'ifreload -a':"
for pair in "${missing[@]}"; do echo; echo "  append:"; stanza "${pair%%:*}" "${pair##*:}"; done
for item in "${upgrade[@]}"; do echo "  add to ${item%%|*}:  ${item#*|}"; done
echo
confirm "Apply the change to the host network?" || die "stopped; nothing changed"

backup="$IFACES.sbx-backup.$(date +%Y%m%d-%H%M%S)"
cp -a "$IFACES" "$backup"
log "backup: $backup"

for pair in "${missing[@]}"; do stanza "${pair%%:*}" "${pair##*:}" >> "$IFACES"; done
for bridge in "$SBX_AGENT_BRIDGE" "$SBX_PERSONAL_BRIDGE"; do
  lines=()
  for item in "${upgrade[@]}"; do [[ "${item%%|*}" == "$bridge" ]] && lines+=("${item#*|}"); done
  [[ ${#lines[@]} -gt 0 ]] && add_lines "$bridge" "${lines[@]}"
done

if ! ifreload -a; then
  cp -a "$backup" "$IFACES"
  ifreload -a || true
  die "ifreload failed; $IFACES was restored from the backup"
fi

for bridge in "$SBX_AGENT_BRIDGE" "$SBX_PERSONAL_BRIDGE"; do
  ip link show "$bridge" >/dev/null || die "$bridge did not come up"
done
[[ "$(cat "/sys/class/net/$SBX_AGENT_BRIDGE/bridge/vlan_filtering" 2>/dev/null)" == 1 ]] \
  || die "$SBX_AGENT_BRIDGE is up but not VLAN-aware; look at its stanza in $IFACES"
log "bridges are up; $SBX_AGENT_BRIDGE is VLAN-aware"
