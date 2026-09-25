#!/usr/bin/env python3
"""The sidecar prototype: three deterministic jobs for ONE sandbox.

  credential proxy   agent0:8080   swaps the sandbox's placeholder for the real token
  expose API         agent0:8081   the sandbox asks for a port
                     net0:8081     the trusted side approves or denies
  port forward       net0:<port> -> vm:<port>, only after an approval

No agent code runs here, and the agent has no shell here. The sandbox proves
who it is with its per-sandbox secret, which works against this sidecar alone.
An approval is accepted from the net0 side only; the firewall limits that side
to the gateway, which is where your Mac arrives from.

Standard library only, like the rest of sbx. tests/sidecar_netns.sh drives it.
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import json
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

# Headers that must not be copied to the upstream: hop-by-hop ones, and the
# sandbox's own credential, which the real one replaces.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "authorization", "x-api-key", "content-length"}
PORT_MIN, PORT_MAX = 1024, 32767


def log(msg: str) -> None:
    print(f"sidecar {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class Config:
    def __init__(self, args):
        self.agent_addr, self.vm_addr, self.net_addr = args.agent_addr, args.vm_addr, args.net_addr
        self.secret = open(args.secret_file).read().strip()
        self.tokens = {}
        for line in open(args.tokens_file):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                self.tokens[k.strip()] = v.strip()
        for name in ("claude", "github"):
            if name not in self.tokens:
                raise SystemExit(f"tokens file: no '{name}=' line")
        self.claude_upstream, self.github_upstream = args.claude_upstream, args.github_upstream
        self.claude_header = args.claude_header
        self.net_expose_port = args.net_expose_port


CFG: Config


def presented_secret(handler: http.server.BaseHTTPRequestHandler) -> bool:
    auth = handler.headers.get("Authorization", "")
    key = handler.headers.get("x-api-key", "")
    return auth == f"Bearer {CFG.secret}" or key == CFG.secret


class Quiet(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # the sidecar logs its own lines
        pass

    def reply(self, code: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""


class Proxy(Quiet):
    """agent0:8080. Every request carries the placeholder; the upstream gets
    the real token. /github/<path> goes to the git host, the rest to Claude."""

    def handle_any(self):
        if not presented_secret(self):
            log(f"proxy {self.command} {self.path} -> 401 (not this sandbox's secret)")
            return self.reply(401, {"error": "unknown sandbox credential"})
        body = self.body()
        if self.path.startswith("/github/"):
            upstream, token, path, header = CFG.github_upstream, CFG.tokens["github"], self.path[len("/github"):], "Authorization"
        else:
            upstream, token, path, header = CFG.claude_upstream, CFG.tokens["claude"], self.path, CFG.claude_header
        u = urllib.parse.urlsplit(upstream)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname, u.port, timeout=120)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Host"] = u.netloc
        headers[header] = f"Bearer {token}" if header.lower() == "authorization" else token
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
    """The exposure requests of this sandbox: port -> state, and the open
    forwards. pending -> approved | denied. Approval opens the listener."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state: dict[int, str] = {}
        self.forwards: dict[int, socket.socket] = {}

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
                self.forwards[port] = start_forward(CFG.net_addr, port, CFG.vm_addr)
                self.state[port] = "approved"
            return "approved"

    def deny(self, port: int) -> str:
        with self.lock:
            if port not in self.state:
                return "unknown"
            srv = self.forwards.pop(port, None)
            if srv is not None:
                # shutdown, not only close: the accept loop holds the socket in
                # another thread, and a plain close would leave it listening
                # until the next connection arrived.
                try:
                    srv.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                srv.close()
            self.state[port] = "denied"
            return "denied"


REQUESTS = Requests()


def pump(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def start_forward(listen_addr: str, port: int, target: str) -> socket.socket:
    """A plain TCP relay net0:<port> -> vm:<port>. Caddy does this with TLS in
    the real design; the relay is enough to prove the policy."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_addr, port))
    srv.listen(16)

    def serve():
        while True:
            try:
                client, peer = srv.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection((target, port), timeout=5)
            except OSError as exc:
                log(f"forward {port}: the sandbox does not answer ({exc})")
                client.close()
                continue
            log(f"forward {port}: {peer[0]} -> {target}")
            threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pump, args=(upstream, client), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return srv


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
            return self.reply(200 if state != "unknown" else 404, {"port": port, "state": state})
        self.reply(404, {"error": "unknown path"})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, side: str = ""):
        self.side = side
        super().__init__(addr, handler)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-addr", required=True, help="this sidecar's address on the private wire (agent0)")
    ap.add_argument("--vm-addr", required=True, help="the sandbox VM's address on that wire")
    ap.add_argument("--net-addr", required=True, help="this sidecar's address on the sidecar network (net0)")
    ap.add_argument("--secret-file", required=True, help="the per-sandbox secret the VM presents")
    ap.add_argument("--tokens-file", required=True, help="lines claude=... and github=...: the real credentials")
    ap.add_argument("--claude-upstream", default="https://api.anthropic.com")
    ap.add_argument("--github-upstream", default="https://api.github.com")
    ap.add_argument("--claude-header", default="Authorization", choices=["Authorization", "x-api-key"],
                    help="Authorization (a bearer token) or x-api-key (a Console API key)")
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
        f"and {CFG.net_addr}:{CFG.net_expose_port} (trusted); forwards go to {CFG.vm_addr}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
