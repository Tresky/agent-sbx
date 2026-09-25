#!/usr/bin/env bash
# Behaviour test for template/files/sbx_mirror.py with a real Caddy. Runs INSIDE
# an Ubuntu 24.04 container (tests/run-mirror-test.sh starts it).
#
# Each claim is a controlled pair: the same request is made before the thing
# that should enable it exists, and after.
set -u
NAME=sbx-test.sbx.internal
IP="$(hostname -I | awk '{print $1}')"
fails=0

check() { # check <pass|fail> <label> <command...>
  local want="$1" label="$2" got=pass; shift 2
  "$@" >/dev/null 2>&1 || got=fail
  if [[ "$got" == "$want" ]]; then printf '  ok    %-58s %s\n' "$label" "$got"
  else printf '  WRONG %-58s got %s, want %s\n' "$label" "$got" "$want"; fails=$((fails + 1)); fi
}
raw_echo() { [[ "$(printf 'ping\n' | nc -w2 "$1" "$2")" == "PONG:ping" ]]; }

# Upstreams. 4400 answers with the request headers it received, so the test can
# see what the proxy added. 4401 is Python's own http.server (the Flask/Django
# family). 5555 is a non-HTTP line protocol. 9222 is on the skip list. 6000
# listens on the wildcard address.
cat > /tmp/echo_headers.py <<'EOF'
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = str(self.headers).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()
EOF
cat > /tmp/raw.py <<'EOF'
import socketserver, sys
class H(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().strip()
        self.wfile.write(b"PONG:" + line + b"\n")
socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()
EOF
python3 /tmp/echo_headers.py 127.0.0.1 4400 & UP4400=$!
python3 -m http.server 4401 --bind 127.0.0.1 >/dev/null 2>&1 &
python3 /tmp/raw.py 127.0.0.1 5555 &
python3 /tmp/raw.py 127.0.0.1 9222 &
python3 /tmp/raw.py 0.0.0.0 6000 &
caddy run --config /sbx/template/files/Caddyfile --adapter caddyfile >/tmp/caddy.log 2>&1 &
sleep 2

echo "== before the mirror starts (control)"
check fail "http://$IP:4400 (upstream is loopback-only)"  curl -sf -m3 "http://$IP:4400/"
check fail "raw $IP:5555"                                  raw_echo "$IP" 5555
check pass "raw $IP:6000 (wildcard listener, direct)"      raw_echo "$IP" 6000

python3 /sbx/template/files/sbx_mirror.py >/tmp/mirror.log 2>&1 & MIRROR=$!
sleep 6

echo "== mirror up, no certificate: everything is a raw forward"
check pass "http://$IP:4400"                               curl -sf -m3 "http://$IP:4400/"
check pass "http://$IP:4401 (python http.server)"          curl -sf -m3 "http://$IP:4401/"
check pass "raw $IP:5555 (non-HTTP protocol)"              raw_echo "$IP" 5555
check fail "raw $IP:9222 (skip list: debug port)"          raw_echo "$IP" 9222
check fail "$IP:2019 (Caddy admin API)"                    curl -sf -m3 "http://$IP:2019/config/"
check pass "raw $IP:6000 still direct"                     raw_echo "$IP" 6000

echo "== certificate installed: HTTP ports move to Caddy with TLS"
mkdir -p /etc/sbx/tls
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=$NAME" -addext "subjectAltName=DNS:$NAME" \
  -keyout /etc/sbx/tls/key.pem -out /etc/sbx/tls/cert.pem >/dev/null 2>&1
sleep 8
TLS=(curl -sf -m4 --cacert /etc/sbx/tls/cert.pem --resolve "$NAME:4400:$IP" --resolve "$NAME:4401:$IP")
check pass "https://$NAME:4400, certificate VERIFIED"      "${TLS[@]}" "https://$NAME:4400/"
check pass "https://$NAME:4401 (python http.server)"       "${TLS[@]}" "https://$NAME:4401/"
headers="$("${TLS[@]}" "https://$NAME:4400/")"
check pass "upstream sees X-Forwarded-Proto: https"        grep -qi '^X-Forwarded-Proto: https' <<<"$headers"
check pass "upstream sees the sandbox name as Host"        grep -qi "^Host: $NAME:4400" <<<"$headers"
code="$(curl -s -m3 -o /dev/null -w '%{http_code}' "http://$IP:4400/")"
check pass "plain http:// on the TLS port redirects (got $code)" test "${code:0:1}" = 3
check pass "raw $IP:5555 is still a raw forward"           raw_echo "$IP" 5555

echo "== upstream stops: the mirror withdraws the port"
kill "$UP4400"; sleep 6
check fail "https://$NAME:4400"                            "${TLS[@]}" "https://$NAME:4400/"
check pass "https://$NAME:4401 is not disturbed"           "${TLS[@]}" "https://$NAME:4401/"

echo "== upstream returns on the same port: no bind collision with the mirror"
python3 /tmp/echo_headers.py 127.0.0.1 4400 & sleep 8
check pass "https://$NAME:4400 again"                      "${TLS[@]}" "https://$NAME:4400/"

echo; echo "--- mirror log"; cat /tmp/mirror.log
kill "$MIRROR" 2>/dev/null
echo
if [[ $fails -eq 0 ]]; then echo "MIRROR TEST PASSED"; else echo "MIRROR TEST FAILED: $fails wrong result(s)"; fi
exit "$fails"
