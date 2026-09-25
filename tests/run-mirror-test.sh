#!/usr/bin/env bash
# Runs tests/mirror_smoke.sh in an Ubuntu 24.04 container with a real Caddy.
# Needs Docker. Run this after every change to template/files/sbx_mirror.py.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker run --rm -v "$PWD:/sbx:ro" ubuntu:24.04 bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null
  apt-get install -y -qq python3 caddy openssl curl netcat-openbsd >/dev/null 2>&1
  bash /sbx/tests/mirror_smoke.sh
'
