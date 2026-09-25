#!/usr/bin/env bash
# Renders gw/nftables.conf.tmpl with the defaults and runs tests/guard_netns.sh
# against it in a privileged Debian container. Needs Docker. Run this after
# every change to the nftables template.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker run --rm --privileged -v "$PWD:/sbx:ro" debian:12 bash -c '
  set -e
  apt-get update -qq >/dev/null
  apt-get install -y -qq nftables iproute2 iputils-ping python3 procps >/dev/null 2>&1
  source /sbx/host/lib.sh
  render /sbx/gw/nftables.conf.tmpl > /tmp/nftables.conf
  bash /sbx/tests/guard_netns.sh /tmp/nftables.conf
'
