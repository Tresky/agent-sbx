#!/usr/bin/env bash
# Prints what a build VM is doing: the provision log, the markers, the running
# processes, and a network check from inside the guest. It goes through the
# guest agent, so it works although the host has no route to a sandbox subnet.
#
#   vm-diag.sh [vmid]      default: the template id
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_pve
ID="${1:?usage: vm-diag.sh <vmid> (the build VM; sbx template list shows it)}"

# The parser lives in a quoted heredoc, so no shell quoting touches it.
read -r -d '' PARSE <<'PY' || true
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except ValueError:
    print(raw.strip())
    sys.exit(0)
sys.stdout.write(d.get("out-data", ""))
sys.stdout.write(d.get("err-data", ""))
print("[exit %s]" % d.get("exitcode"))
PY

# guest <command>: run it in the guest, print its stdout, stderr and exit code.
guest() {
  qm guest exec "$ID" --timeout 60 -- bash -c "$1" 2>&1 | python3 -c "$PARSE" || true
}

log "VM $ID: $(qm status "$ID")"
log "provision log, last 40 lines"
guest 'tail -n 40 /var/log/sbx-provision.log'
log "markers in /var/lib/sbx"
guest 'ls -la /var/lib/sbx/'
log "longest-running processes"
guest "ps -eo pid,etime,args --sort=-etime | grep -v '\\[' | head -20"
log "network from inside the guest: the download that the rvm installer makes"
guest 'sudo -u dev -H curl -sS --fail -L -o /dev/null -w "%{http_code} %{size_download} bytes from %{url_effective}\n" https://github.com/rvm/rvm/archive/1.29.12.tar.gz'
log "rvm state for the user dev"
guest 'ls -d /home/dev/.rvm 2>&1; sudo -u dev -H gpg --list-keys --with-colons 2>/dev/null | grep -c "^pub" | sed "s/^/gpg keys imported: /"'
log "cloud-init"
guest 'cloud-init status --long 2>&1 | head -5'
