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
import collections
import http.client
import http.server
import json
import os
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
CHUNK = 64 * 1024  # the proxy streams in pieces of this size; it never holds a whole body
# What a git host answers when this credential does not reach this repository.
# The proxy then tries the sandbox's next token.
REFUSED = {401, 403, 404}
GitToken = collections.namedtuple("GitToken", "name upstream user token")


def log(msg: str) -> None:
    print(f"sidecar {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class Config:
    def __init__(self, args):
        self.agent_addr, self.vm_addr, self.net_addr = args.agent_addr, args.vm_addr, args.net_addr
        self.secret = open(args.secret_file).read().strip()
        # `github` is the git token for whatever host --github-upstream names
        # (GitHub, GitLab, ...); `git_user` is the user name sent with it.
        self.tokens = {"claude": "", "github": "", "git_user": ""}
        # `git.<name>=<upstream> <user> <token>`: more git tokens, by name. One
        # sandbox can hold several, for one host or for several.
        self.named_git: list[GitToken] = []
        for line in open(args.tokens_file):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                if k.startswith("git."):
                    parts = v.split()
                    if len(parts) == 3:
                        self.named_git.append(GitToken(k[4:], *parts))
                    else:  # the name only: never the value
                        log(f"tokens: {k} is not <upstream> <user> <token>; ignored")
                else:
                    self.tokens[k] = v.strip()
        self.claude_upstream, self.github_upstream = args.claude_upstream, args.github_upstream
        # (host, repository, git service) -> the token that worked for it.
        self.git_choices: dict[tuple[str, str, str], GitToken | None] = {}
        self.net_expose_port = args.net_expose_port
        if not self.secret:
            raise SystemExit("the secret file is empty")

    def git_tokens_for(self, host: str) -> list[GitToken]:
        """Every token for this host, in the order to try them. A token never
        goes to another host."""
        out = [t for t in self.named_git if netloc(t.upstream) == host]
        if self.tokens["github"] and netloc(self.github_upstream) == host:
            out.insert(0, GitToken("project", self.github_upstream, self.tokens["git_user"], self.tokens["github"]))
        return out

    def remember_git(self, key: tuple[str, str, str], token: GitToken | None) -> None:
        if len(self.git_choices) >= 4096:  # one sandbox's repositories; a bound, not a cache policy
            self.git_choices.clear()
        self.git_choices[key] = token

    def chosen_git(self, host: str, repo: str, service: str) -> GitToken | None:
        """The token for a request that cannot be tried twice: its body streams
        once. git always asks for /info/refs first, which finds the token."""
        for svc in [service] if service else ["git-receive-pack", "git-upload-pack"]:
            if (host, repo, svc) in self.git_choices:
                return self.git_choices[(host, repo, svc)]
        tokens = self.git_tokens_for(host)
        return tokens[0] if tokens else None


CFG: Config


def netloc(upstream: str) -> str:
    return urllib.parse.urlsplit(upstream).netloc


def git_request(command: str, path: str) -> tuple[str, str, bool]:
    """(the repository, the git service, whether each token may be tried).
    Only git's first request qualifies: a GET has no body to send twice."""
    plain, _, query = path.partition("?")
    for tail in ("/git-upload-pack", "/git-receive-pack"):
        if plain.endswith(tail):
            return plain[:-len(tail)].removesuffix(".git"), tail[1:], False
    if plain.endswith("/info/refs"):
        service = urllib.parse.parse_qs(query).get("service", [""])[0]
        return plain[:-len("/info/refs")].removesuffix(".git"), service, command == "GET"
    return plain.split(".git/", 1)[0], "", False


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


def git_auth(token: str, headers: dict, user: str = "") -> None:
    """What git over HTTPS sends to its host: the token as the password.
    GitHub ignores the user name; GitLab wants any non-blank one."""
    login = f"{user or 'x-access-token'}:{token}"
    headers["Authorization"] = "Basic " + base64.b64encode(login.encode()).decode()


def local_location(location: str, upstream: str, prefix: str) -> str:
    """A redirect from the git host back into the proxy. GitLab answers
    /<repo>/info/refs with a 301 to https://gitlab.com/<repo>.git/info/refs;
    git follows it and makes every later request there, past the proxy, where
    the sandbox's firewall stops it. A path-only Location keeps git here."""
    u = urllib.parse.urlsplit(upstream)
    origin = f"{u.scheme}://{u.netloc}"
    if location == origin or location.startswith(origin + "/"):
        return prefix + location[len(origin):]
    return location


class Proxy(Quiet):
    """<wire>:8080. Every request carries the placeholder; the upstream gets
    the real token. /git/<host>/<path> goes to that git host, if the sandbox has
    a token for it; /github/<path> to the host of the project's token; the rest
    to Claude."""

    def handle_any(self):
        if not presented_secret(self):
            log(f"proxy {self.command} {self.path} -> 401 (not this sandbox's secret)")
            # git asks first with no credential. Its HTTP library sends the
            # stored one only when the 401 names a scheme, as GitHub's does;
            # without this header git retries empty-handed, gets a second 401,
            # and deletes the placeholder from ~/.git-credentials.
            return self.reply(401, {"error": "unknown sandbox credential"},
                              {"WWW-Authenticate": 'Basic realm="sbx sidecar"'})
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        if self.path.startswith("/github/"):
            return self.handle_git("/github", netloc(CFG.github_upstream), CFG.github_upstream, headers)
        if self.path.startswith("/git/"):
            host = self.path[len("/git/"):].partition("/")[0]
            tokens = CFG.git_tokens_for(host)
            if not tokens:  # this proxy is not a way out to any host at all
                log(f"proxy {self.command} {self.path} -> 403 (no token for this host)")
                return self.reply(403, {"error": f"this sandbox has no git token for {host}"})
            return self.handle_git(f"/git/{host}", host, tokens[0].upstream, headers)
        upstream, token, path = CFG.claude_upstream, CFG.tokens["claude"], self.path
        if token:
            claude_auth(token, headers)
        body, chunked = self.request_body()
        try:
            conn, resp = self.open_upstream(upstream, path, headers, body, chunked)
        except OSError as exc:
            log(f"proxy {self.command} {path} -> 502 ({exc})")
            return self.reply(502, {"error": f"upstream: {exc}"})
        try:
            sent = self.relay(resp)
        finally:
            conn.close()
        log(f"proxy {self.command} {path} -> {resp.status} ({sent} bytes)")

    def handle_git(self, prefix: str, host: str, upstream: str, headers: dict) -> None:
        """git's first request tries each token of this host, then none, as a
        public repository needs none. The one that works serves the requests
        that follow. If none works, the sandbox gets the host's answer."""
        path = self.path[len(prefix):]
        repo, service, try_each = git_request(self.command, path)
        if try_each:
            choices, (body, chunked) = [*CFG.git_tokens_for(host), None], (None, False)
        else:
            choices, (body, chunked) = [CFG.chosen_git(host, repo, service)], self.request_body()
        for i, choice in enumerate(choices):
            sent_headers = dict(headers)
            if choice is not None:
                git_auth(choice.token, sent_headers, choice.user)
            label = choice.name if choice is not None else "no token"
            try:
                conn, resp = self.open_upstream(upstream, path, sent_headers, body, chunked)
            except OSError as exc:
                log(f"proxy {self.command} {path} ({label}) -> 502 ({exc})")
                return self.reply(502, {"error": f"upstream: {exc}"})
            if resp.status in REFUSED and i < len(choices) - 1:
                log(f"proxy {self.command} {path} ({label}) -> {resp.status}; the next one")
                conn.close()
                continue
            break
        if try_each and resp.status not in REFUSED:
            CFG.remember_git((host, repo, service), choice)
        try:
            sent = self.relay(resp, upstream, prefix)
        finally:
            conn.close()
        log(f"proxy {self.command} {path} ({label}) -> {resp.status} ({sent} bytes)")

    def open_upstream(self, upstream: str, path: str, headers: dict, body, chunked: bool):
        """(the connection, the response). Both ways are STREAMED, in CHUNK
        pieces: a sidecar has little memory, and a git clone or push of a large
        repository is hundreds of MB."""
        u = urllib.parse.urlsplit(upstream)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname, u.port, timeout=300)
        headers["Host"] = u.netloc
        if chunked:
            headers["Transfer-Encoding"] = "chunked"
        elif body is not None:
            headers["Content-Length"] = self.headers["Content-Length"]
        try:
            conn.request(self.command, path, body=body, headers=headers, encode_chunked=chunked)
            return conn, conn.getresponse()
        except OSError:
            conn.close()
            raise

    def request_body(self):
        """(an iterator of the request body's pieces, or None; whether it is chunked)."""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._chunks(), True
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None, False

        def pieces(left=length):
            while left:
                data = self.rfile.read(min(CHUNK, left))
                if not data:
                    raise OSError("the sandbox closed the request early")
                left -= len(data)
                yield data
        return pieces(), False

    def _chunks(self):
        """The pieces of a chunked request body (git push sends one)."""
        while True:
            size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass  # trailers
                return
            left = size
            while left:
                data = self.rfile.read(min(CHUNK, left))
                if not data:
                    raise OSError("the sandbox closed the request early")
                left -= len(data)
                yield data
            self.rfile.readline()  # the CRLF after each chunk

    def relay(self, resp, git_upstream: str | None = None, git_prefix: str = "/github") -> int:
        """The upstream's answer to the sandbox, piece by piece. Returns the bytes.
        With git_upstream, a redirect to that host comes back into the proxy,
        and a 401 goes on as a 403. git DELETES a credential that gets a 401,
        here the placeholder, so one repository that no token reaches would
        break every git command after it. The placeholder was right: the
        host's refusal is about the real tokens, and git need not forget it."""
        git, upstream = git_upstream is not None, git_upstream
        self.send_response(403 if git and resp.status == 401 else resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length", "keep-alive"):
                if git and k.lower() == "www-authenticate":
                    continue
                if git and k.lower() == "location":
                    v = local_location(v, upstream, git_prefix)
                self.send_header(k, v)
        no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
        length = resp.getheader("Content-Length")
        if no_body:
            if length is not None:
                self.send_header("Content-Length", length)
            self.end_headers()
            return 0
        if length is not None:
            self.send_header("Content-Length", length)
        else:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        sent = 0
        while data := resp.read(CHUNK):
            sent += len(data)
            if length is None:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            else:
                self.wfile.write(data)
        if length is None:
            self.wfile.write(b"0\r\n\r\n")
        return sent

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any


class Requests:
    """The exposure requests of this sandbox: port -> state.
    pending -> approved | denied. An approval opens the port in the firewall.

    The approved ports are also kept in a file, one per line: sbx-sidecar-apply
    renders the firewall afresh (at every boot, and on every policy change) and
    then restarts this service, which opens them again (restore). A pending
    request is not kept: the sandbox asks again."""

    def __init__(self, path: str = ""):
        self.lock = threading.Lock()
        self.state: dict[int, str] = {}
        self.path = path

    def _save(self) -> None:
        if not self.path:
            return
        ports = sorted(p for p, s in self.state.items() if s == "approved")
        tmp = f"{self.path}.new"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write("".join(f"{p}\n" for p in ports))
            os.replace(tmp, self.path)
        except OSError as exc:
            # The firewall already changed; only the memory of it is lost.
            log(f"cannot keep the approved ports in {self.path}: {exc}")

    def restore(self) -> list[int]:
        """Open the saved ports again. A line that is not a port in the range,
        or is the sidecar's own, is skipped: the file is re-checked as input."""
        try:
            with open(self.path) as fh:
                lines = fh.read().split()
        except (OSError, TypeError):
            return []
        opened = []
        with self.lock:
            for line in lines:
                if not line.isdigit():
                    continue
                port = int(line)
                if not PORT_MIN <= port <= PORT_MAX or port in RESERVED:
                    continue
                if open_port(port):
                    self.state[port] = "approved"
                    opened.append(port)
        return opened

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
                self._save()
            return "approved"

    def deny(self, port: int) -> str:
        with self.lock:
            if port not in self.state:
                return "unknown"
            if self.state[port] == "approved":
                close_port(port)
            self.state[port] = "denied"
            self._save()
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
    ap.add_argument("--tokens-file", required=True, help="lines claude=..., github=... and git_user=...: the real credentials")
    ap.add_argument("--claude-upstream", default="https://api.anthropic.com")
    ap.add_argument("--github-upstream", default="https://github.com")
    ap.add_argument("--net-expose-port", type=int, default=8081,
                    help="the trusted side's port; only a loopback test needs it to differ")
    ap.add_argument("--approved-file", default="/etc/sbx/sidecar/approved",
                    help="the approved ports, kept across restarts and reboots ('' keeps none)")
    args = ap.parse_args(argv)
    global CFG
    CFG = Config(args)
    REQUESTS.path = args.approved_file
    if reopened := REQUESTS.restore():
        log("approved ports opened again: " + ", ".join(map(str, reopened)))

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
