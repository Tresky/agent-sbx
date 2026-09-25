#!/usr/bin/env bash
# Behaviour test for `30-template-build.sh --finish`, in a Debian container with
# a FAKE qm and the REAL seal.sh. Needs Docker. Two runs form a controlled pair:
#   1. the VM is up with BOTH markers (the seal failed after every check
#      passed): --finish must seal and convert.
#   2. the VM is up with the failed marker only: --finish must refuse.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker run --rm -v "$PWD:/sbx:ro" debian:12 bash -c '
set -u
# Without python3 the script under test cannot read a single guest reply, and
# its watch loop then waits the full stall deadline: a package failure must
# end the test at once, not look like a hang. A full Docker disk shows up as
# "invalid signature" from apt.
if ! { apt-get update -qq >/dev/null && apt-get install -y -qq python3 procps >/dev/null 2>&1; }; then
  echo "TEST SETUP FAILED: apt-get could not install python3 and procps in the container."
  echo "  A full Docker disk looks like this; check with: docker system df"
  exit 2
fi
mkdir -p /stub /tmp/fake /var/lib/sbx /var/log /usr/local/lib/sbx
cp -r /sbx /work

# --- stubs for what the host script and the seal call ---------------------
cat > /stub/qm <<"EOF"
#!/bin/bash
cmd="$1"; shift
case "$cmd" in
  status)   [[ -e /tmp/fake/stopped ]] && echo "status: stopped" || echo "status: running" ;;
  config)   printf "name: sbx-base-test\ncicustom: user=local:snippets/x\n" ;;
  guest)
    shift 2   # exec, vmid
    pass=0
    while [[ "$1" != "--" ]]; do [[ "$1" == "--pass-stdin" ]] && pass="$2"; shift; done
    shift
    if [[ "$pass" == 1 ]]; then out="$("$@" 2>&1)"; else out="$("$@" 2>&1 </dev/null)"; fi
    code=$?
    python3 -c "import json,sys; print(json.dumps({\"exitcode\": int(sys.argv[1]), \"exited\": 1, \"out-data\": sys.argv[2]}, indent=3))" "$code" "$out" ;;
  set|template) echo "qm $cmd $*" >> /tmp/fake/calls ;;
esac
EOF
cat > /stub/systemd-run <<"EOF"
#!/bin/bash
while [[ "$1" == --* ]]; do shift; done
nohup bash "$@" >/tmp/fake/seal.log 2>&1 &
EOF
cat > /stub/poweroff <<"EOF"
#!/bin/bash
touch /tmp/fake/stopped
EOF
for s in cloud-init apt-get journalctl fstrim pvesh; do printf "#!/bin/bash\nexit 0\n" > /stub/$s; done
printf "#!/bin/bash\necho /tmp/fake/snippet.yaml\n" > /stub/pvesm
chmod +x /stub/*
export PATH="/stub:$PATH"
export SEAL_WAIT_S=5
echo "1234567890abcdef" > /etc/machine-id
printf "=== checks ===\nall good\n=== seal ===\nFailed to find executable /run/sbx-seal.sh: Permission denied\nPROVISION FAILED at line 197\n" > /var/log/sbx-provision.log

fails=0
check() { if eval "$2"; then echo "  ok    $1"; else echo "  WRONG $1"; fails=$((fails + 1)); fi; }

echo "== run 1: both markers (the real state after build #3)"
touch /var/lib/sbx/provision.ok /var/lib/sbx/provision.failed
rm -f /tmp/fake/stopped /tmp/fake/calls
out="$(bash /work/host/30-template-build.sh --finish 2>&1)"; code=$?
echo "$out" | sed "s/^/    /"
check "exit 0"                                  "[[ $code -eq 0 ]]"
check "the seal was placed, executable"         "[[ -x /usr/local/lib/sbx/seal.sh ]]"
check "the real seal ran: machine-id emptied"   "[[ ! -s /etc/machine-id ]]"
check "the VM powered off"                      "[[ -e /tmp/fake/stopped ]]"
check "cicustom deleted, ciuser set, templated" "grep -q -- \"--delete cicustom\" /tmp/fake/calls && grep -q -- \"--ciuser dev\" /tmp/fake/calls && grep -q \"^qm template 9000\" /tmp/fake/calls"
check "reports the template as ready"           "grep -q \"is ready\" <<<\"$out\""

echo "== run 2 (control): the failed marker only"
rm -f /var/lib/sbx/provision.ok /tmp/fake/stopped /tmp/fake/calls
echo "1234567890abcdef" > /etc/machine-id
out="$(bash /work/host/30-template-build.sh --finish 2>&1)"; code=$?
check "exit non-zero"                           "[[ $code -ne 0 ]]"
check "says provision FAILED"                   "grep -q \"provision FAILED\" <<<\"$out\""
check "no seal ran: machine-id intact"          "[[ -s /etc/machine-id ]]"
check "nothing converted"                       "[[ ! -e /tmp/fake/calls ]]"

echo
if [[ $fails -eq 0 ]]; then echo "FINISH TEST PASSED"; else echo "FINISH TEST FAILED: $fails wrong result(s)"; fi
exit $fails
'
