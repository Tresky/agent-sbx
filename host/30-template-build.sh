#!/usr/bin/env bash
# Builds the base VM template. Run after 20-gw-create.sh: the build VM gets its
# address and its internet access from the gateway.
#
# The host holds no address on a sandbox bridge, so it cannot SSH to the build
# VM. cloud-init carries the provision payload in and runs it; the VM powers
# itself off when the provision succeeds and STAYS UP when it fails.
#
#   30-template-build.sh            build; refuses if the template id is taken
#   30-template-build.sh --replace  destroy the old template first
#   30-template-build.sh --finish   attach to a build VM that is already up:
#                                   watch the provision, seal, convert. For an
#                                   SSH session that dropped mid-build, or a
#                                   build whose seal step failed.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_pve

ID="$SBX_TEMPLATE_VMID"
NAME="sbx-base-$(date +%Y%m%d)"
TIMEOUT_MIN=120
# A compile is silent for minutes; nothing in the provision is silent for 45.
STALL_WARN_MIN=15
STALL_DIE_MIN=45
SEAL_PATH=/usr/local/lib/sbx/seal.sh
# How long a VM with the ok marker may stay up before the seal is run from
# here. The self-seal needs 15 s plus the shutdown.
SEAL_WAIT_S="${SEAL_WAIT_S:-120}"
SNIPPET_NAME="sbx-template-user-data.yaml"
MODE="${1:-}"

# --- guest helpers ------------------------------------------------------------
# The guest agent is the only path into the build VM. Every helper tolerates
# an agent that is not up yet and a reply that is not JSON.
guest_json() { qm guest exec "$ID" --timeout 15 -- bash -c "$1" 2>/dev/null || true; }
guest_out()  { guest_json "$1" | python3 -c 'import json,sys
try: sys.stdout.write(json.load(sys.stdin).get("out-data", ""))
except Exception: pass' 2>/dev/null || true; }
guest_ok()   { guest_json "$1" | python3 -c 'import json,sys
try: sys.exit(0 if json.load(sys.stdin).get("exitcode") == 0 else 1)
except Exception: sys.exit(1)' 2>/dev/null; }
show_log()   { echo; echo "--- /var/log/sbx-provision.log, last 40 lines"; guest_out 'tail -n 40 /var/log/sbx-provision.log' | sed 's/^/    /'; echo; }
vm_stopped() { qm status "$ID" | grep -q stopped; }

# Blocks until the VM powers off (the provision sealed itself), or until the
# provision FAILED, stalled, or timed out. Prints each new log line.
watch_provision() {
  local deadline last="" quiet_since warned=0 now line quiet
  deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 )); quiet_since=$(date +%s)
  while :; do
    vm_stopped && return 0
    now=$(date +%s)
    # The ok marker is written only after every check passed, so it wins over
    # a failed marker: a build whose SEAL failed has both, and is finished.
    guest_ok 'test -e /var/lib/sbx/provision.ok' && return 0
    line="$(guest_out 'tail -n 1 /var/log/sbx-provision.log' | tr -d '\r' | cut -c1-160)"
    if [[ -n "$line" && "$line" != "$last" ]]; then echo "    $line"; last="$line"; quiet_since=$now; warned=0; fi
    if guest_ok 'test -e /var/lib/sbx/provision.failed'; then
      show_log
      die "provision FAILED; VM $ID stays up. More detail: bash $SBX_HOST_DIR/vm-diag.sh $ID"
    fi
    quiet=$(( (now - quiet_since) / 60 ))
    if (( quiet >= STALL_WARN_MIN && warned == 0 )); then
      warn "no new log line for $quiet min; the VM's longest-running processes:"
      guest_out "ps -eo etime,args --sort=-etime | grep -v '\\[' | head -6" | sed 's/^/    /'
      warned=1
    fi
    if (( quiet >= STALL_DIE_MIN )); then
      show_log
      die "no new log line for $quiet min; the build is stuck. VM $ID stays up. More detail: bash $SBX_HOST_DIR/vm-diag.sh $ID"
    fi
    if (( now > deadline )); then
      show_log
      die "timeout after ${TIMEOUT_MIN} min; VM $ID stays up. More detail: bash $SBX_HOST_DIR/vm-diag.sh $ID"
    fi
    sleep 20
  done
}

# The provision ran the seal itself when it could. When the VM is still up
# with the ok marker, the seal is run from here with the CURRENT seal.sh.
seal_if_needed() {
  # The provision schedules its own seal 15 s after the ok marker, and the
  # guest agent stops answering while the VM powers off. An agent with no
  # answer is therefore NOT a missing marker: only an agent that answers
  # "absent" is. Wait for the power-off before the seal is run from here.
  local waited=0
  while (( waited < SEAL_WAIT_S )); do
    vm_stopped && return 0
    if guest_ok 'test ! -e /var/lib/sbx/provision.ok'; then
      die "VM $ID is up but has no provision.ok marker; it did not finish. More detail: bash $SBX_HOST_DIR/vm-diag.sh $ID"
    fi
    sleep 5; waited=$((waited + 5))
  done
  vm_stopped && return 0
  log "the provision finished but the VM did not power off; running the seal"
  qm guest exec "$ID" --pass-stdin 1 -- bash -c "cat > $SEAL_PATH && chmod 0755 $SEAL_PATH" \
    < "$SBX_ROOT_DIR/template/seal.sh" >/dev/null
  guest_ok "test -x $SEAL_PATH" || die "could not place $SEAL_PATH in the guest"
  # A unit name that is new each time: systemd refuses a name that exists.
  local unit
  unit="sbx-seal-$(date +%s)"
  guest_ok "systemd-run --unit=$unit --on-active=3s $SEAL_PATH" \
    || die "systemd-run refused the seal; look in the guest: journalctl -u $unit"
  for _ in $(seq 1 60); do
    vm_stopped && return 0
    sleep 5
  done
  die "the VM did not power off within 5 minutes of the seal"
}

convert() {
  log "converting to a template"
  # The clone must not inherit the build payload, and must not run a full apt
  # upgrade on its first boot (ciupgrade defaults to on).
  qm config "$ID" | grep -q '^cicustom:' && qm set "$ID" --delete cicustom
  qm set "$ID" --ciuser "$SBX_VM_USER" --ciupgrade 0 --ipconfig0 ip=dhcp
  rm -f "$(pvesm path "$SBX_SNIPPET_STORAGE:snippets/$SNIPPET_NAME" 2>/dev/null || true)"
  qm template "$ID"
  # A destroyed VM takes its ACLs with it. Give the CLI's token its access to
  # the new template back, when that token has been set up.
  if pveum user list --output-format json 2>/dev/null | grep -q '"userid":"sbx@pve"'; then
    log "giving the sbx token its access to the template again"
    bash "$SBX_HOST_DIR/40-api-token.sh" --acl-only
  fi
  log "template $ID ($(qm config "$ID" | sed -n 's/^name: //p')) is ready"
}

# --- --finish: attach to a build VM that is already up -------------------------
if [[ "$MODE" == "--finish" ]]; then
  qm status "$ID" >/dev/null 2>&1 || die "VM $ID does not exist; run this script without --finish"
  qm config "$ID" | grep -q '^template: 1' && die "VM $ID is already a template"
  log "attaching to VM $ID"
  watch_provision
  seal_if_needed
  convert
  exit 0
fi

# --- a new build ----------------------------------------------------------------
if qm status "$ID" >/dev/null 2>&1; then
  [[ "$MODE" == "--replace" ]] || die "VM $ID exists; pass --replace to rebuild it, or --finish to complete it"
  # Proxmox refuses to destroy a template that linked clones still use.
  confirm "Destroy VM $ID and build it again?" || die "stopped"
  qm stop "$ID" >/dev/null 2>&1 || true
  qm destroy "$ID" --purge 1 \
    || die "could not destroy $ID; remove its linked clones first (sbx list), or set a new SBX_TEMPLATE_VMID"
fi

for extra in ${SBX_TEMPLATE_EXTRAS:-}; do
  [[ -f "$SBX_ROOT_DIR/template/extras/$extra.sh" ]] \
    || die "SBX_TEMPLATE_EXTRAS names '$extra', but template/extras/$extra.sh does not exist"
done

pct status "$SBX_GW_CTID" 2>/dev/null | grep -q running \
  || die "the gateway container $SBX_GW_CTID is not running; the build VM would get no address"

log "cloud image"
mkdir -p "$SBX_IMAGE_STORAGE_DIR"
IMG="$SBX_IMAGE_STORAGE_DIR/$(basename "$SBX_UBUNTU_IMAGE_URL")"
# -N: download only when the server copy is newer.
wget -q --show-progress -N -P "$SBX_IMAGE_STORAGE_DIR" "$SBX_UBUNTU_IMAGE_URL"

log "snippet storage"
if ! pvesh get "/storage/$SBX_SNIPPET_STORAGE" --output-format json | grep -q snippets; then
  content="$(pvesh get "/storage/$SBX_SNIPPET_STORAGE" --output-format json \
             | python3 -c 'import json,sys; print(json.load(sys.stdin)["content"])')"
  confirm "Enable 'snippets' on storage $SBX_SNIPPET_STORAGE (content: $content)?" || die "stopped"
  pvesm set "$SBX_SNIPPET_STORAGE" --content "$content,snippets"
fi
SNIPPET_PATH="$(pvesm path "$SBX_SNIPPET_STORAGE:snippets/$SNIPPET_NAME")"
mkdir -p "$(dirname "$SNIPPET_PATH")"

log "payload"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/root"
cp -a "$SBX_ROOT_DIR/template" "$stage/root/template"
# Every SBX_ variable, quoted, so provision.sh sees this host's overrides.
( set -o posix; set ) | grep -E '^SBX_[A-Z_]+=' > "$stage/root/build.conf"
tar -C "$stage/root" -czf "$stage/payload.tgz" .

{
  cat <<EOF
#cloud-config
hostname: sbx-template
package_update: true
packages:
  - qemu-guest-agent
write_files:
  - path: /opt/sbx/payload.tgz
    permissions: '0600'
    encoding: b64
    content: $(base64 -w0 "$stage/payload.tgz")
runcmd:
  - systemctl enable --now qemu-guest-agent
  - mkdir -p /opt/sbx/payload
  - tar -xzf /opt/sbx/payload.tgz -C /opt/sbx/payload
  - bash /opt/sbx/payload/template/provision.sh
EOF
} > "$SNIPPET_PATH"

log "creating VM $ID ($NAME)"
# q35 + OVMF so that a clone can take a PCIe GPU later without a rebuild.
qm create "$ID" --name "$NAME" --ostype l26 \
  --machine q35 --bios ovmf \
  --efidisk0 "$SBX_VM_STORAGE:0,efitype=4m,pre-enrolled-keys=0" \
  --cpu host --cores "$SBX_TEMPLATE_CORES" --memory "$SBX_TEMPLATE_MEMORY_MB" \
  --scsihw virtio-scsi-single \
  --scsi0 "$SBX_VM_STORAGE:0,import-from=$IMG,discard=on,ssd=1,iothread=1" \
  --ide2 "$SBX_VM_STORAGE:cloudinit" --boot order=scsi0 \
  --serial0 socket \
  --agent enabled=1,fstrim_cloned_disks=1 \
  --net0 "virtio,bridge=$SBX_AGENT_BRIDGE" \
  --ipconfig0 ip=dhcp \
  --tags sbx-template
qm disk resize "$ID" scsi0 "${SBX_TEMPLATE_DISK_GB}G"
qm set "$ID" --cicustom "user=$SBX_SNIPPET_STORAGE:snippets/$SNIPPET_NAME"

log "starting the build VM"
qm start "$ID"

# Fail EARLY when the network is the problem. With no network the VM never gets
# a guest agent, and the watch loop would wait the full timeout in silence. The
# gateway's lease file is the proof that DHCP reached the VM; it is matched by
# MAC, because the hostname in the first request is the thing under test.
mac="$(qm config "$ID" | sed -n 's/^net0:.*=\([0-9A-Fa-f:]\{17\}\),.*/\1/p' | tr 'A-F' 'a-f')"
[[ -n "$mac" ]] || die "could not read the MAC address of net0"
lease=""
for _ in $(seq 1 60); do
  lease="$(pct exec "$SBX_GW_CTID" -- grep -i " $mac " /var/lib/misc/dnsmasq.leases 2>/dev/null || true)"
  [[ -n "$lease" ]] && break
  sleep 3
done
[[ -n "$lease" ]] || die "the build VM ($mac) got no DHCP lease from the gateway in 3 minutes. \
Look at its console: qm terminal $ID   Then check: pct exec $SBX_GW_CTID -- journalctl -u dnsmasq -n 30"
read -r _ _ lease_ip lease_name _ <<<"$lease"
log "DHCP works: $lease_ip, hostname in the request: '$lease_name'"
answer="$(pct exec "$SBX_GW_CTID" -- dig +short "$lease_name.$SBX_DOMAIN" "@${SBX_AGENT_NET}.1" 2>/dev/null || true)"
if [[ "$answer" == "$lease_ip" ]]; then
  log "DNS works: $lease_name.$SBX_DOMAIN -> $answer"
else
  warn "dnsmasq does not answer for $lease_name.$SBX_DOMAIN (got: '${answer:-nothing}'); the name mechanism needs a look"
fi

log "provisioning; this takes 15 to 40 minutes"
watch_provision
seal_if_needed
convert
