#!/usr/bin/env bash
# Creates the scoped API token that the Mac tool uses for every daily
# operation. Run after 30-template-build.sh. Idempotent, except for the token
# secret, which Proxmox shows ONE time.
#
#   40-api-token.sh            create the pool, the user, the roles, the ACLs, the token
#   40-api-token.sh --rotate   delete the token and create a new one
#   40-api-token.sh --acl-only apply the roles and the ACLs again, and stop
#   40-api-token.sh --emit     with or without --rotate: print only the line
#                              SBX_TOKEN=<token>, for `sbx setup` to capture
#   --token-id NAME            the token to make or rotate; the default is "cli".
#                              `sbx setup` gives each Mac its own token, so a
#                              rotation on one Mac does not stop the others
#
# What the token can do, and where:
#   /pool/$SBX_POOL                 manage VMs (clone into, configure, start, snapshot, destroy)
#   /pool/$SBX_TEMPLATE_POOL        clone and read each template; NOT change or destroy one
#   /storage/$SBX_VM_STORAGE        allocate disk space
#   the two sandbox bridges         attach a NIC
# What it cannot do: reach a VM outside the pool, the gateway container, the
# host shell, or ANY OTHER BRIDGE. The last one protects the rule that the
# gateway's firewall rests on: a sandbox NIC can only sit on a sandbox bridge.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_pve

USER_ID="sbx@pve"
TOKEN_ID="cli"

ROTATE=0 ACL_ONLY=0 EMIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rotate) ROTATE=1 ;;
    --acl-only) ACL_ONLY=1 ;;
    --emit) EMIT=1 ;;
    --token-id) [[ $# -ge 2 ]] || die "--token-id needs a name"; TOKEN_ID="$2"; shift ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done
[[ "$TOKEN_ID" =~ ^[A-Za-z][A-Za-z0-9._-]*$ ]] || die "the token id '$TOKEN_ID' has a character that Proxmox refuses"
# With --emit, stdout carries the one SBX_TOKEN line; the progress goes to stderr.
if [[ $EMIT -eq 1 ]]; then exec 3>&1 1>&2; fi

COMMON="VM.Allocate VM.Clone VM.Audit VM.PowerMgmt VM.Snapshot VM.Snapshot.Rollback \
VM.Config.Network VM.Config.CPU VM.Config.Memory VM.Config.Cloudinit VM.Config.Options \
VM.Config.Disk VM.Config.HWType Pool.Audit"

set_role() { # set_role <name> <privs...>
  local name="$1"; shift
  pveum role add "$name" --privs "$*" 2>/dev/null || pveum role modify "$name" --privs "$*"
}

log "roles"
# Reading the guest agent (for a VM's address) is VM.GuestAgent.Audit on
# Proxmox 9 and VM.Monitor on Proxmox 8. A role with an unknown privilege is
# refused, so the newer name is tried first.
# shellcheck disable=SC2086
set_role SbxOperator $COMMON VM.GuestAgent.Audit 2>/dev/null \
  || set_role SbxOperator $COMMON VM.Monitor
set_role SbxTemplateUser VM.Clone VM.Audit
set_role SbxStorage Datastore.AllocateSpace Datastore.Audit
set_role SbxBridge SDN.Use

log "pool and user"
pveum pool add "$SBX_POOL" --comment "sbx sandboxes" 2>/dev/null || true
pveum pool add "$SBX_TEMPLATE_POOL" --comment "sbx templates" 2>/dev/null || true
pveum user add "$USER_ID" --comment "sbx command line tool" 2>/dev/null || true

log "permissions"
acl() { pveum acl modify "$1" --users "$USER_ID" --roles "$2"; }
acl "/pool/$SBX_POOL" SbxOperator
acl "/pool/$SBX_TEMPLATE_POOL" SbxTemplateUser
acl "/storage/$SBX_VM_STORAGE" SbxStorage
acl "/sdn/zones/localnetwork/$SBX_AGENT_BRIDGE" SbxBridge
acl "/sdn/zones/localnetwork/$SBX_PERSONAL_BRIDGE" SbxBridge
if [[ -n "${SBX_GPU_MAPPING:-}" ]]; then
  set_role SbxMapping Mapping.Use
  acl "/mapping/pci/$SBX_GPU_MAPPING" SbxMapping
fi

if [[ $ACL_ONLY -eq 1 ]]; then
  log "access entries applied; the token is unchanged"
  exit 0
fi

if pveum user token list "$USER_ID" --output-format json | grep -q "\"tokenid\":\"$TOKEN_ID\""; then
  if [[ $ROTATE -eq 1 ]]; then
    pveum user token remove "$USER_ID" "$TOKEN_ID"
  else
    log "the token $USER_ID!$TOKEN_ID exists; its secret cannot be shown again. Use --rotate for a new one."
    TOKEN_EXISTS=1
  fi
fi

if [[ -n "${TOKEN_EXISTS:-}" && $EMIT -eq 1 ]]; then
  die "the token exists and its secret cannot be shown again; pass --rotate"
fi

if [[ -z "${TOKEN_EXISTS:-}" ]]; then
  log "token"
  # privsep 0: the token has the permissions of its user. The USER is the
  # narrow part; a second, narrower layer would only duplicate the ACLs above.
  secret="$(pveum user token add "$USER_ID" "$TOKEN_ID" --privsep 0 --output-format json \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])')"
  if [[ $EMIT -eq 1 ]]; then
    printf 'SBX_TOKEN=%s!%s=%s\n' "$USER_ID" "$TOKEN_ID" "$secret" >&3
    exit 0
  fi
  echo
  echo "Store the token in the macOS keychain. Run this ON YOUR MAC:"
  echo
  echo "  security add-generic-password -U -s sbx-pve-token -a sbx -w '$USER_ID!$TOKEN_ID=$secret'"
  echo
fi

echo "Then set these in ~/.config/sbx/config.toml:"
echo
echo "  pve_api = \"https://$(hostname -I | awk '{print $1}'):8006\""
echo "  pve_token_command = [\"security\", \"find-generic-password\", \"-s\", \"sbx-pve-token\", \"-w\"]"
echo
echo "The host certificate is self-signed. Choose ONE way to verify it:"
echo
echo "  A. pve_ca_file = \"~/.config/sbx/pve-root-ca.crt\""
echo "     Survives a certificate renewal. Needs the address in pve_api to be one of:"
openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -ext subjectAltName 2>/dev/null | tail -n +2 | sed 's/^ */       /'
echo "     Save the certificate below as that file:"
echo
cat /etc/pve/pve-root-ca.pem
echo
echo "  B. pve_fingerprint = \"$(openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256 | cut -d= -f2)\""
echo "     Works with any address. Must be updated when Proxmox renews the certificate."
