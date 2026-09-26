#!/usr/bin/env bash
# Builds a template VM from a template definition (templates/<name>.toml, or
# templates/local/<name>.toml). Run after 20-gw-create.sh: the build VM gets
# its address and its internet access from the gateway.
#
# The host holds no address on a sandbox bridge, so it cannot SSH to the build
# VM. cloud-init carries the provision payload in and runs it; the VM powers
# itself off when the provision succeeds and STAYS UP when it fails.
#
# Each build is a NEW version under a free id in the template range, named
# sbx-tpl-<name>-<date>. The old versions stay for the sandboxes that are
# linked clones of them; a version with no clones left is removed.
#
#   30-template-build.sh <name>                 build a new version
#   30-template-build.sh --finish <name> [id]   attach to a build VM that is up:
#                                               watch, seal, convert. For an SSH
#                                               session that dropped mid-build
#   30-template-build.sh --prune [<name>]       remove the old versions that no
#                                               sandbox uses any more
#   30-template-build.sh --rm <name>            remove every version that no
#                                               sandbox uses
#   30-template-build.sh --adopt <id> <name>    make a template from before named
#                                               templates the first version of <name>
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_pve

TIMEOUT_MIN=120
# A compile is silent for minutes; nothing in the provision is silent for 45.
STALL_WARN_MIN=15
STALL_DIE_MIN=45
SEAL_PATH=/usr/local/lib/sbx/seal.sh
# How long a VM with the ok marker may stay up before the seal is run from
# here. The self-seal needs 15 s plus the shutdown.
SEAL_WAIT_S="${SEAL_WAIT_S:-120}"
MODE="${1:-}"
ID=""
TPL=""

valid_name() { [[ "$1" =~ ^[a-z][a-z0-9-]{0,23}$ ]] || die "'$1' is not a template name"; }
snippet_name() { echo "sbx-template-$1.yaml"; }

# Every VM, one per line: vmid <TAB> template (0/1) <TAB> name <TAB> tags (;-separated)
vms() {
  pvesh get /cluster/resources --type vm --output-format json 2>/dev/null | python3 -c 'import json,sys
try: rows = json.load(sys.stdin)
except Exception: rows = []
for r in rows:
    if r.get("type") == "qemu":
        tags = (r.get("tags") or "").replace(",", ";")
        print("%s\t%s\t%s\t%s" % (r["vmid"], int(bool(r.get("template"))), r.get("name", ""), tags))'
}
has_tag() { [[ ";$1;" == *";$2;"* ]]; }

# The versions of one template, oldest first: vmid <TAB> name
versions_of() {
  local name="$1" vmid t vname tags
  # `if`, not an && chain: under `set -e` and pipefail, a chain that fails on
  # the last VM would end the loop, and the script, with its status.
  while IFS=$'\t' read -r vmid t vname tags; do
    if [[ "$t" == 1 ]] && has_tag "$tags" sbx-template && has_tag "$tags" "sbx-tpl-$name"; then
      printf '%s\t%s\n' "$vmid" "$vname"
    fi
  done < <(vms) | sort -t$'\t' -k2,2 -k1,1n
}

# The number of VMs whose disks are linked clones of template <vmid>. A linked
# clone's volume names its base: base-<vmid>-disk-N, on every storage type.
clones_of() {
  local vmid="$1"
  { grep -lE "base-${vmid}-disk-[0-9]+" "${SBX_PVE_NODES_DIR:-/etc/pve/nodes}"/*/qemu-server/*.conf 2>/dev/null || true; } \
    | { grep -v "/${vmid}.conf$" || true; } | wc -l | tr -d ' '
}

# Removes the versions of <name> that no sandbox uses. With keep_newest=1 the
# newest version stays even when nothing uses it: it is the one that
# `sbx new` clones.
prune() {
  local name="$1" keep_newest="$2" all newest vmid vname n
  all="$(versions_of "$name")"
  [[ -n "$all" ]] || return 0
  newest="$(tail -n1 <<<"$all" | cut -f1)"
  while IFS=$'\t' read -r vmid vname; do
    if [[ "$keep_newest" == 1 && "$vmid" == "$newest" ]]; then continue; fi
    n="$(clones_of "$vmid")"
    if [[ "$n" -eq 0 ]]; then
      log "removing $vname ($vmid): no sandbox uses it"
      qm destroy "$vmid" --purge 1 || warn "could not remove $vmid"
    else
      log "keeping $vname ($vmid): $n sandbox(es) use it"
    fi
  done <<<"$all"
}

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


# Converts the sealed build VM $ID of template $TPL, with fingerprint $2.
convert() {
  local hash="$1"
  log "converting to a template"
  # The clone must not inherit the build payload, and must not run a full apt
  # upgrade on its first boot (ciupgrade defaults to on).
  qm config "$ID" | grep -q '^cicustom:' && qm set "$ID" --delete cicustom
  qm set "$ID" --ciuser "$SBX_VM_USER" --ciupgrade 0 --ipconfig0 ip=dhcp
  rm -f "$(pvesm path "$SBX_SNIPPET_STORAGE:snippets/$(snippet_name "$ID")" 2>/dev/null || true)"
  qm template "$ID"
  qm set "$ID" --tags "sbx-template;sbx-tpl-$TPL;sbx-h-$hash"
  # The token clones and reads every template through this pool's ACL, so a
  # new version needs no ACL of its own.
  pveum pool add "$SBX_TEMPLATE_POOL" --comment "sbx templates" 2>/dev/null || true
  pveum pool modify "$SBX_TEMPLATE_POOL" --vms "$ID"
  if pveum user list --output-format json 2>/dev/null | grep -q '"userid":"sbx@pve"'; then
    bash "$SBX_HOST_DIR/40-api-token.sh" --acl-only >/dev/null
  fi
  log "template $TPL: $(qm config "$ID" | sed -n 's/^name: //p') ($ID) is ready"
  prune "$TPL" 1
}

case "$MODE" in
  --prune)
    if [[ -n "${2:-}" ]]; then
      valid_name "$2"; prune "$2" 1
    else
      while IFS=$'\t' read -r _ t _ tags; do
        [[ "$t" == 1 ]] || continue
        for tag in ${tags//;/ }; do if [[ "$tag" == sbx-tpl-* ]]; then echo "${tag#sbx-tpl-}"; fi; done
      done < <(vms) | sort -u | while read -r name; do prune "$name" 1; done
    fi
    exit 0 ;;
  --rm)
    [[ -n "${2:-}" ]] || die "usage: $0 --rm <name>"
    valid_name "$2"
    prune "$2" 0
    [[ -z "$(versions_of "$2")" ]] || die "template $2 stays: sandboxes use it. Remove them first (sbx list)"
    exit 0 ;;
  --adopt)
    [[ -n "${2:-}" && -n "${3:-}" ]] || die "usage: $0 --adopt <id> <name>"
    ID="$2"; TPL="$3"; valid_name "$TPL"
    qm config "$ID" 2>/dev/null | grep -q '^template: 1' || die "VM $ID is not a template"
    old="$(qm config "$ID" | sed -n 's/^name: //p')"
    stamp="$(sed -n 's/^sbx-base-\([0-9]\{8\}\)$/\1-0000/p' <<<"$old")"
    qm set "$ID" --name "sbx-tpl-$TPL-${stamp:-$(date +%Y%m%d-%H%M)}" --tags "sbx-template;sbx-tpl-$TPL;sbx-h-legacy"
    pveum pool add "$SBX_TEMPLATE_POOL" --comment "sbx templates" 2>/dev/null || true
    pveum pool modify "$SBX_TEMPLATE_POOL" --vms "$ID"
    # The token had a right on this one id before; the pool gives it now.
    pveum acl delete "/vms/$ID" --users sbx@pve --roles SbxTemplateUser 2>/dev/null || true
    if pveum user list --output-format json 2>/dev/null | grep -q '"userid":"sbx@pve"'; then
      bash "$SBX_HOST_DIR/40-api-token.sh" --acl-only >/dev/null
    fi
    log "VM $ID ($old) is now the first version of template $TPL. Rebuild it when you can: sbx template rebuild $TPL"
    exit 0 ;;
  --finish)
    [[ -n "${2:-}" ]] || die "usage: $0 --finish <name> [id]"
    TPL="$2"; valid_name "$TPL"; ID="${3:-}"
    if [[ -z "$ID" ]]; then
      ID="$(vms | while IFS=$'\t' read -r vmid t _ tags; do
              if [[ "$t" == 0 ]] && has_tag "$tags" sbx-building && has_tag "$tags" "sbx-tpl-$TPL"; then echo "$vmid"; fi
            done | tail -n1)"
      [[ -n "$ID" ]] || die "no build of template $TPL is running; run this script without --finish"
    fi
    qm status "$ID" >/dev/null 2>&1 || die "VM $ID does not exist; run this script without --finish"
    qm config "$ID" | grep -q '^template: 1' && die "VM $ID is already a template"
    hash="$(qm config "$ID" | sed -n 's/^tags:.*sbx-h-\([0-9a-z]*\).*/\1/p')"
    log "attaching to VM $ID (template $TPL)"
    watch_provision
    seal_if_needed
    convert "${hash:-unknown}"
    exit 0 ;;
  ""|--*)
    die "usage: $0 <name> | --finish <name> [id] | --prune [<name>] | --rm <name> | --adopt <id> <name>" ;;
esac

# --- a new build ----------------------------------------------------------------
TPL="$MODE"; valid_name "$TPL"
envtext="$(python3 "$SBX_ROOT_DIR/sbxlib/templates.py" env "$TPL")" || die "the definition of template $TPL is not valid"
eval "$envtext"
unset envtext
log "template $TPL: components: ${SBX_COMPONENTS:-none}; fingerprint $SBX_TEMPLATE_HASH"

pct status "$SBX_GW_CTID" 2>/dev/null | grep -q running \
  || die "the gateway container $SBX_GW_CTID is not running; the build VM would get no address"

# An earlier build of this template that failed stays up for its log. A new
# build replaces it, so that failed builds do not use up the id range.
vms | while IFS=$'\t' read -r vmid t vname tags; do
  if [[ "$t" == 0 ]] && has_tag "$tags" sbx-building && has_tag "$tags" "sbx-tpl-$TPL"; then
    log "removing the earlier build VM $vmid ($vname) of template $TPL"
    qm stop "$vmid" >/dev/null 2>&1 || true
    qm destroy "$vmid" --purge 1 || warn "could not remove VM $vmid"
  fi
done

for id in $(seq "$SBX_TEMPLATE_VMID_MIN" "$SBX_TEMPLATE_VMID_MAX"); do
  if pvesh get /cluster/nextid --vmid "$id" >/dev/null 2>&1; then ID="$id"; break; fi
done
[[ -n "$ID" ]] || die "no free id in $SBX_TEMPLATE_VMID_MIN-$SBX_TEMPLATE_VMID_MAX; run: $0 --prune"
NAME="sbx-tpl-$TPL-$(date +%Y%m%d-%H%M)"
SNIPPET_NAME="$(snippet_name "$ID")"

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
# The sidecar component installs the files of sidecar/.
[[ -d "$SBX_ROOT_DIR/sidecar" ]] && cp -a "$SBX_ROOT_DIR/sidecar" "$stage/root/sidecar"
# Every SBX_ variable, quoted: this host's settings and the definition's.
write_build_conf "$stage/root/build.conf"
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
  --tags "sbx-template;sbx-tpl-$TPL;sbx-h-$SBX_TEMPLATE_HASH;sbx-building"
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
convert "$SBX_TEMPLATE_HASH"
