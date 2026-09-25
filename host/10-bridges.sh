#!/usr/bin/env bash
# Creates the two sandbox bridges on the Proxmox host. Idempotent.
#
# A sandbox bridge has no physical port and the host takes no address on it.
# The only way out of a sandbox subnet is the gateway container, which is where
# every firewall rule lives.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_pve

IFACES=/etc/network/interfaces

stanza() {
  cat <<EOF

auto $1
iface $1 inet manual
	bridge-ports none
	bridge-stp off
	bridge-fd 0
#sbx: $2 sandbox subnet. The gateway container $SBX_GW_CTID routes it.
EOF
}

missing=()
for pair in "$SBX_AGENT_BRIDGE:agent" "$SBX_PERSONAL_BRIDGE:personal"; do
  bridge="${pair%%:*}"
  if grep -qE "^iface ${bridge} " "$IFACES"; then
    log "$bridge is already in $IFACES"
  else
    missing+=("$pair")
  fi
done

if [[ ${#missing[@]} -eq 0 ]]; then
  log "nothing to do"
  exit 0
fi

echo "This script appends these stanzas to $IFACES and runs 'ifreload -a':"
for pair in "${missing[@]}"; do stanza "${pair%%:*}" "${pair##*:}"; done
echo
confirm "Apply the change to the host network?" || die "stopped; nothing changed"

backup="$IFACES.sbx-backup.$(date +%Y%m%d-%H%M%S)"
cp -a "$IFACES" "$backup"
log "backup: $backup"

for pair in "${missing[@]}"; do stanza "${pair%%:*}" "${pair##*:}" >> "$IFACES"; done

if ! ifreload -a; then
  cp -a "$backup" "$IFACES"
  ifreload -a || true
  die "ifreload failed; $IFACES was restored from the backup"
fi

for pair in "${missing[@]}"; do
  ip link show "${pair%%:*}" >/dev/null || die "${pair%%:*} did not come up"
done
log "bridges are up"
