#!/usr/bin/env bash
# Renders gw/nftables.conf.tmpl and sidecar/nftables.conf.tmpl with the
# defaults and runs tests/sidecar_netns.sh against them in a privileged Debian
# container. Needs Docker. Run it after every change to the sidecar prototype
# or to either firewall template.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker run --rm --privileged -v "$PWD:/sbx:ro" debian:12 bash -c '
  set -e
  apt-get update -qq >/dev/null
  apt-get install -y -qq nftables iproute2 iputils-ping python3 procps curl >/dev/null 2>&1
  source /sbx/host/lib.sh
  SBX_SIDECAR_LINK=10.79.0; export SBX_SIDECAR_LINK
  render /sbx/gw/nftables.conf.tmpl > /tmp/gw.conf
  render /sbx/sidecar/nftables.conf.tmpl > /tmp/sidecar.conf
  bash /sbx/tests/sidecar_netns.sh /tmp/gw.conf /tmp/sidecar.conf
'
