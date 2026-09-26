#!/usr/bin/env python3
"""The sidecar service: two deterministic jobs for ONE sandbox.

  credential proxy   <wire>:8080   swaps the sandbox's placeholder for the real token
  expose API         <wire>:8081   the sandbox asks for a port
                     <net>:8081    the trusted side approves or denies

No agent code runs here, and the agent has no shell here. The sandbox proves
who it is with its per-sandbox secret, which works against this sidecar alone,
as a bearer token, an x-api-key header, or the password of HTTP basic auth
(what git sends). An approval is accepted from the net side only; the firewall
limits that side to the gateway, which is where your Mac arrives from.

An approved port is opened in the firewall, not relayed here: the port joins
the nftables set `approved` in table `sbx_sidecar_nat`, whose prerouting
chain translates it to the sandbox VM (sidecar/nftables.conf.tmpl).

Standard library only, like the rest of sbx. sbx-sidecar-apply starts it with
the addresses of one sandbox; tests/sidecar_netns.sh drives it in namespaces.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import http.server
import json
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

# Headers that must not be copied to the upstream: hop-by-hop ones, and the
# sandbox's own credential, which the real one replaces.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "authorization", "x-api-key", "content-length"}
PORT_MIN, PORT_MAX = 1024, 32767
# The sidecar's own sshd and expose API: in the range, but never the VM's.
# The firewall keeps them too (nftables.conf.tmpl).
RESERVED = {2222, 8081}
# Claude Code sends this beta header with a subscription (OAuth) token. The
# proxy adds it when the real token is one, because Claude Code in the sandbox
# only sees a placeholder and does not know. Unverified with the real API.
OAUTH_BETA = "oauth-2025-04-20"
NFT_SET = ("ip", "sbx_sidecar_nat", "approved")


def log(msg: str) -> None:
    print(f"sidecar {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class Config:
    def __init__(self, args):
        self.agent_addr, self.vm_addr, self.net_addr = args.agent_addr, args.vm_addr, args.net_addr
        self.secret = open(args.secret_file).read().strip()
        self.tokens = {"claude": "", "github": ""}
        for line in open(args.tokens_file):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                self.tokens[k.strip()] = v.strip()
        self.claude_upstream, self.github_upstream = args.claude_upstream, args.github_upstream
        self.net_expose_port = args.net_expose_port
        if not self.secret:
            raise SystemExit("the secret file is empty")


CFG: Config


def presented_secret(handler: http.server.BaseHTTPRequestHandler) -> bool:
    auth = handler.headers.get("Authorization", "")
    if auth == f"Bearer {CFG.secret}" or handler.headers.get("x-api-key", "") == CFG.secret:
        return True
    if auth.startswith("Basic "):
        try:
            _, _, password = base64.b64decode(auth[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return password == CFG.secret
    return False


def nft(action: str, port: int) -> bool:
    """add or delete one port in the approved set. Returns whether nft agreed."""
    family, table, name = NFT_SET
    done = subprocess.run(["nft", action, "element", family, table, name, f"{{ {port} }}"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        log(f"nft {action} {port}: {done.stderr.strip()}")
    return done.returncode == 0


def open_port(port: int) -> bool:
    return nft("add", port)


def close_port(port: int) -> bool:
    return nft("delete", port)


class Quiet(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # the sidecar logs its own lines
        pass

    def reply(self, code: int, obj, headers: dict | None = None) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""


def claude_auth(token: str, headers: dict) -> None:
    """The header that the real Claude credential goes in. A Console API key
    goes in x-api-key; anything else is a bearer token."""
    if token.startswith("sk-ant-api"):
        headers["x-api-key"] = token
        return
    headers["Authorization"] = f"Bearer {token}"
    if token.startswith("sk-ant-oat"):
        betas = [b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()]
        if OAUTH_BETA not in betas:
            betas.append(OAUTH_BETA)
        headers["anthropic-beta"] = ",".join(betas)


def git_auth(token: str, headers: dict) -> None:
    """What git over HTTPS sends to GitHub: the token as the password."""
    headers["Authorization"] = "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


class Proxy(Quiet):
    """<wire>:8080. Every request carries the placeholder; the upstream gets
    the real token. /github/<path> goes to the git host, the rest to Claude."""

    def handle_any(self):
        if not presented_secret(self):
            log(f"proxy {self.command} {self.path} -> 401 (not this sandbox's secret)")
            # git asks first with no credential. Its HTTP library sends the
            # stored one only when the 401 names a scheme, as GitHub's does;
            # without this header git retries empty-handed, gets a second 401,
            # and deletes the placeholder from ~/.git-credentials.
            return self.reply(401, {"error": "unknown sandbox credential"},
                              {"WWW-Authenticate": 'Basic realm="sbx sidecar"'})
        body = self.body()
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        if self.path.startswith("/github/"):
            upstream, token, path = CFG.github_upstream, CFG.tokens["github"], self.path[len("/github"):]
            if token:
                git_auth(token, headers)
        else:
            upstream, token, path = CFG.claude_upstream, CFG.tokens["claude"], self.path
            if token:
                claude_auth(token, headers)
        u = urllib.parse.urlsplit(upstream)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname, u.port, timeout=300)
        headers["Host"] = u.netloc
        try:
            conn.request(self.command, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except OSError as exc:
            log(f"proxy {self.command} {path} -> 502 ({exc})")
            return self.reply(502, {"error": f"upstream: {exc}"})
        finally:
            conn.close()
        log(f"proxy {self.command} {path} -> {resp.status} ({len(data)} bytes)")
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any


class Requests:
    """The exposure requests of this sandbox: port -> state.
    pending -> approved | denied. An approval opens the port in the firewall."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state: dict[int, str] = {}

    def snapshot(self):
        with self.lock:
            return [{"port": p, "state": s} for p, s in sorted(self.state.items())]

    def ask(self, port: int) -> str:
        with self.lock:
            if self.state.get(port) != "approved":
                self.state[port] = "pending"
            return self.state[port]

    def approve(self, port: int) -> str:
        with self.lock:
            if port not in self.state:
                return "unknown"
            if self.state[port] != "approved":
                if not open_port(port):
                    return "error"
                self.state[port] = "approved"
            return "approved"

    def deny(self, port: int) -> str:
        with self.lock:
            if port not in self.state:
                return "unknown"
            if self.state[port] == "approved":
                close_port(port)
            self.state[port] = "denied"
            return "denied"


REQUESTS = Requests()


class Expose(Quiet):
    """The same handler on both sides; self.server.side says which. The
    sandbox may ask. Only the trusted side may approve or deny."""

    def do_GET(self):
        if self.path == "/requests":
            return self.reply(200, {"requests": REQUESTS.snapshot()})
        self.reply(404, {"error": "unknown path"})

    def do_POST(self):
        side = self.server.side
        try:
            obj = json.loads(self.body() or b"{}")
        except ValueError:
            return self.reply(400, {"error": "the body must be JSON"})
        port = obj.get("port") if isinstance(obj, dict) else None
        if not isinstance(port, int) or isinstance(port, bool) or not (PORT_MIN <= port <= PORT_MAX):
            return self.reply(400, {"error": f"port must be an integer from {PORT_MIN} to {PORT_MAX}"})
        if port in RESERVED | {CFG.net_expose_port}:
            return self.reply(400, {"error": f"port {port} is the sidecar's own"})
        if self.path == "/expose":
            if side != "agent":
                return self.reply(403, {"error": "the sandbox asks for its own ports"})
            if not presented_secret(self):
                return self.reply(401, {"error": "unknown sandbox credential"})
            state = REQUESTS.ask(port)
            log(f"expose {port}: {state}")
            return self.reply(202, {"port": port, "state": state})
        if self.path in ("/approve", "/deny"):
            if side != "net":
                log(f"{self.path[1:]} {port}: refused from the sandbox side")
                return self.reply(403, {"error": "only the trusted side approves"})
            state = REQUESTS.approve(port) if self.path == "/approve" else REQUESTS.deny(port)
            log(f"{self.path[1:]} {port}: {state}")
            code = {"unknown": 404, "error": 500}.get(state, 200)
            return self.reply(code, {"port": port, "state": state})
        self.reply(404, {"error": "unknown path"})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, side: str = ""):
        self.side = side
        super().__init__(addr, handler)

    # HTTPServer.server_bind asks DNS for the fully qualified name of the
    # address before it listens. With no resolver in reach, that waits out the
    # resolver's timeouts, and the port refuses connections meanwhile. The
    # name is only used in CGI variables, so the sidecar skips the lookup.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-addr", required=True, help="this sidecar's address on the wire to its sandbox")
    ap.add_argument("--vm-addr", required=True, help="the sandbox VM's address on that wire")
    ap.add_argument("--net-addr", required=True, help="this sidecar's address on the sidecar network")
    ap.add_argument("--secret-file", required=True, help="the per-sandbox secret the VM presents")
    ap.add_argument("--tokens-file", required=True, help="lines claude=... and github=...: the real credentials")
    ap.add_argument("--claude-upstream", default="https://api.anthropic.com")
    ap.add_argument("--github-upstream", default="https://github.com")
    ap.add_argument("--net-expose-port", type=int, default=8081,
                    help="the trusted side's port; only a loopback test needs it to differ")
    args = ap.parse_args(argv)
    global CFG
    CFG = Config(args)

    servers = [
        Server((CFG.agent_addr, 8080), Proxy),
        Server((CFG.agent_addr, 8081), Expose, side="agent"),
        Server((CFG.net_addr, CFG.net_expose_port), Expose, side="net"),
    ]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    log(f"up: proxy {CFG.agent_addr}:8080, expose {CFG.agent_addr}:8081 (sandbox) "
        f"and {CFG.net_addr}:{CFG.net_expose_port} (trusted); approved ports go to {CFG.vm_addr}; "
        f"claude token {'set' if CFG.tokens['claude'] else 'none'}, git token {'set' if CFG.tokens['github'] else 'none'}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
