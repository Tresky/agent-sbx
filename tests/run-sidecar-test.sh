#!/usr/bin/env bash
# Runs tests/sidecar_netns.sh in a privileged Debian container: the gateway's
# real rules, the sidecar's real rules and service, and the VLAN-per-sandbox
# wiring, in network namespaces. Needs Docker. Run it after every change under
# sidecar/ or to either firewall template.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker run --rm --privileged -v "$PWD:/sbx:ro" debian:12 bash -c '
  set -e
  apt-get update -qq >/dev/null
  apt-get install -y -qq nftables iproute2 iputils-ping python3 procps curl >/dev/null 2>&1
  source /sbx/host/lib.sh
  render /sbx/gw/nftables.conf.tmpl > /tmp/gw.conf
  bash /sbx/tests/sidecar_netns.sh /tmp/gw.conf /sbx/sidecar/nftables.conf.tmpl
'
