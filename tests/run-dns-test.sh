#!/usr/bin/env bash
# Renders gw/dnsmasq.conf.tmpl and runs tests/dns_netns.sh against it in a
# privileged Debian 13 container (the gateway's own distribution). Needs Docker.
# Run this after every change to the dnsmasq template.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker run --rm --privileged -v "$PWD:/sbx:ro" debian:13 bash -c '
  set -e
  export LANG=C.UTF-8 LC_ALL=C.UTF-8
  apt-get update -qq >/dev/null
  apt-get install -y -qq dnsmasq-base dnsutils iproute2 udhcpc >/dev/null 2>&1
  source /sbx/host/lib.sh
  SBX_DNSMASQ_SERVERS="server=127.0.0.99"; export SBX_DNSMASQ_SERVERS
  render /sbx/gw/dnsmasq.conf.tmpl > /tmp/dnsmasq.conf
  bash /sbx/tests/dns_netns.sh /tmp/dnsmasq.conf "$SBX_DOMAIN"
'
