"""sidecar/sidecar.py, driven without a network: each handler gets an
in-memory socket, the upstream is a fake connection class, and the nftables
calls are stubs. tests/run-sidecar-test.sh proves the same flows on real
interfaces; this file proves the handlers' decisions."""
from __future__ import annotations

import base64
import contextlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
import sidecar  # noqa: E402

SECRET = "placeholder-for-sandbox-A"


class FakeSock:
    """What a request handler needs from a connection: a readable file of
    the request, and sendall() for the response."""

    def __init__(self, raw: bytes):
        self.rx = io.BytesIO(raw)
        self.tx = bytearray()

    def makefile(self, mode, bufsize=-1):
        return self.rx

    def sendall(self, data):
        self.tx += data

    def setsockopt(self, *args):
        pass

    def settimeout(self, value):
        pass

    def close(self):
        pass


class FakeResponse:
    def __init__(self, status: int, body: bytes, length: bool = False, extra=()):
        self.status, self._body, self._length, self._extra = status, body, length, list(extra)

    def read(self, n=None):
        n = len(self._body) if n is None else n
        out, self._body = self._body[:n], self._body[n:]
        return out

    def getheaders(self):
        return [("Content-Type", "application/json"), ("Transfer-Encoding", "chunked")] + self._extra

    def getheader(self, name, default=None):
        if name.lower() == "content-length" and self._length:
            return str(len(self._body))
        return default


class FakeUpstream:
    """Stands in for http.client.HTTPConnection. Records the one request."""
    calls: list[dict] = []
    status = 200
    headers: list[tuple[str, str]] = []
    # With it: the status for each request's credential (None: no credential).
    status_for = None

    def __init__(self, host, port=None, timeout=None):
        self.host, self.port = host, port

    response_body = b'{"ok": true}'
    response_length = False

    def request(self, method, path, body=None, headers=None, encode_chunked=False):
        if body is not None and not isinstance(body, (bytes, str)):
            body = b"".join(body)          # the proxy streams it; the fake gathers it
        FakeUpstream.calls.append({"host": self.host, "port": self.port, "method": method, "path": path,
                                   "body": body, "headers": dict(headers or {}), "chunked": encode_chunked})

    def getresponse(self):
        status = FakeUpstream.status
        if FakeUpstream.status_for is not None:
            status = FakeUpstream.status_for(FakeUpstream.calls[-1])
        return FakeResponse(status, FakeUpstream.response_body, FakeUpstream.response_length, FakeUpstream.headers)

    def close(self):
        pass


def request(handler_cls, side: str, method: str, path: str, headers: dict | None = None,
            body: bytes = b"") -> tuple[int, dict, bytes]:
    lines = [f"{method} {path} HTTP/1.1", "Host: sidecar"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    sock = FakeSock(raw)
    server = types.SimpleNamespace(side=side)
    handler_cls(sock, ("10.79.0.2", 40000), server)
    head, _, out_body = bytes(sock.tx).partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode().split("\r\n")
    status = int(status_line.split()[1])
    out_headers = {k.lower(): v for k, v in (h.split(": ", 1) for h in header_lines)}
    if out_headers.get("transfer-encoding") == "chunked":
        out_body = dechunk(out_body)
    return status, out_headers, out_body


def dechunk(raw: bytes) -> bytes:
    out, rest = b"", raw
    while True:
        size_line, _, rest = rest.partition(b"\r\n")
        size = int(size_line, 16)
        if size == 0:
            return out
        out, rest = out + rest[:size], rest[size + 2:]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "secret").write_text(SECRET + "\n")
        (d / "tokens").write_text("claude=sk-ant-oat01-REAL\ngithub=ghp_REAL\n")
        self.args = types.SimpleNamespace(
            agent_addr="10.79.0.1", vm_addr="10.79.0.2", net_addr="10.77.0.57",
            secret_file=str(d / "secret"), tokens_file=str(d / "tokens"),
            claude_upstream="http://203.0.113.10:9000", github_upstream="http://203.0.113.10:9001",
            net_expose_port=8081)
        sidecar.CFG = sidecar.Config(self.args)
        sidecar.REQUESTS = sidecar.Requests()
        FakeUpstream.calls = []
        FakeUpstream.response_body = b'{"ok": true}'
        FakeUpstream.response_length = False
        FakeUpstream.status = 200
        FakeUpstream.status_for = None
        FakeUpstream.headers = []
        self._real = (sidecar.http.client.HTTPConnection, sidecar.open_port, sidecar.close_port)
        sidecar.http.client.HTTPConnection = FakeUpstream
        self.nft: list[tuple[str, int]] = []
        self.nft_ok = True
        sidecar.open_port = lambda port: self.nft.append(("add", port)) or self.nft_ok
        sidecar.close_port = lambda port: self.nft.append(("delete", port)) or True

    def tearDown(self):
        sidecar.http.client.HTTPConnection, sidecar.open_port, sidecar.close_port = self._real
        self.tmp.cleanup()

    def set_tokens(self, text: str):
        Path(self.args.tokens_file).write_text(text)
        sidecar.CFG = sidecar.Config(self.args)


class ProxyTest(Base):
    def test_claude_call_gets_the_real_subscription_token_and_its_beta_header(self):
        status, _, body = request(sidecar.Proxy, "agent", "POST", "/v1/messages",
                                  {"Authorization": f"Bearer {SECRET}", "anthropic-version": "2023-06-01",
                                   "anthropic-beta": "prompt-caching-2024-07-31", "Content-Type": "application/json"},
                                  b'{"model": "x"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        [call] = FakeUpstream.calls
        self.assertEqual((call["host"], call["port"], call["path"]), ("203.0.113.10", 9000, "/v1/messages"))
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-ant-oat01-REAL")
        self.assertEqual(call["headers"]["anthropic-beta"], "prompt-caching-2024-07-31,oauth-2025-04-20")
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(call["headers"]["Host"], "203.0.113.10:9000")
        self.assertEqual(call["body"], b'{"model": "x"}')
        self.assertNotIn(SECRET, repr(call))

    def test_an_api_key_goes_in_x_api_key_with_no_beta_header(self):
        self.set_tokens("claude=sk-ant-api03-KEY\ngithub=\n")
        request(sidecar.Proxy, "agent", "GET", "/v1/models", {"Authorization": f"Bearer {SECRET}"})
        [call] = FakeUpstream.calls
        self.assertEqual(call["headers"]["x-api-key"], "sk-ant-api03-KEY")
        self.assertNotIn("Authorization", call["headers"])
        self.assertNotIn("anthropic-beta", call["headers"])

    def test_git_call_goes_to_the_git_host_with_basic_auth(self):
        # git presents the placeholder as a basic-auth password.
        basic = base64.b64encode(f"sbx:{SECRET}".encode()).decode()
        status, _, _ = request(sidecar.Proxy, "agent", "GET", "/github/o/r.git/info/refs?service=git-upload-pack",
                               {"Authorization": f"Basic {basic}"})
        self.assertEqual(status, 200)
        [call] = FakeUpstream.calls
        self.assertEqual((call["port"], call["path"]), (9001, "/o/r.git/info/refs?service=git-upload-pack"))
        want = "Basic " + base64.b64encode(b"x-access-token:ghp_REAL").decode()
        self.assertEqual(call["headers"]["Authorization"], want)
        self.assertNotIn("x-api-key", call["headers"])

    def test_no_git_token_means_no_credential_upstream(self):
        self.set_tokens("claude=sk-ant-oat01-REAL\ngithub=\n")
        request(sidecar.Proxy, "agent", "GET", "/github/o/public.git/info/refs", {"x-api-key": SECRET})
        [call] = FakeUpstream.calls
        self.assertNotIn("Authorization", call["headers"])

    def test_the_bound_user_name_goes_with_the_git_token(self):
        # GitLab wants a non-blank user name; the binding's is sent as it is.
        self.set_tokens("claude=\ngithub=glpat-REAL\ngit_user=oauth2\n")
        request(sidecar.Proxy, "agent", "GET", "/github/g/p.git/info/refs", {"x-api-key": SECRET})
        [call] = FakeUpstream.calls
        self.assertEqual(call["headers"]["Authorization"],
                         "Basic " + base64.b64encode(b"oauth2:glpat-REAL").decode())

    def test_a_git_host_redirect_stays_inside_the_proxy(self):
        """GitLab sends /g/p/info/refs to /g/p.git/info/refs with an absolute
        301. Passed on as it is, git would go to the host itself, past the
        proxy, and the sandbox's firewall would stop it."""
        FakeUpstream.status = 301
        FakeUpstream.headers = [("Location", "http://203.0.113.10:9001/g/p.git/info/refs?service=git-upload-pack")]
        status, headers, _ = request(sidecar.Proxy, "agent", "GET", "/github/g/p/info/refs?service=git-upload-pack",
                                     {"x-api-key": SECRET})
        self.assertEqual(status, 301)
        self.assertEqual(headers["location"], "/github/g/p.git/info/refs?service=git-upload-pack")
        # Controlled pair: a redirect to another host is left alone, and so is
        # a Location on the Claude side.
        for loc, path in (("https://cdn.example.com/x", "/github/g/p.git/x"),
                          ("http://203.0.113.10:9001/y", "/v1/models")):
            FakeUpstream.headers = [("Location", loc)]
            _, headers, _ = request(sidecar.Proxy, "agent", "GET", path, {"x-api-key": SECRET})
            self.assertEqual(headers["location"], loc)

    def test_wrong_or_missing_placeholder_is_refused_before_any_upstream_call(self):
        wrong_basic = base64.b64encode(b"sbx:wrong").decode()
        for headers in ({"Authorization": "Bearer wrong"}, {"x-api-key": "wrong"},
                        {"Authorization": f"Basic {wrong_basic}"}, {"Authorization": "Basic %%%"}, {}):
            status, got, body = request(sidecar.Proxy, "agent", "GET", "/v1/models", headers)
            self.assertEqual(status, 401, headers)
            self.assertIn("unknown sandbox credential", body.decode())
            # git's HTTP library sends a stored credential only after a 401
            # that names the scheme; without it, git deletes the credential.
            self.assertEqual(got.get("www-authenticate"), 'Basic realm="sbx sidecar"')
        self.assertEqual(FakeUpstream.calls, [])

    def test_upstream_status_and_hop_headers(self):
        FakeUpstream.status = 429
        FakeUpstream.response_length = True
        status, headers, body = request(sidecar.Proxy, "agent", "GET", "/v1/models",
                                        {"Authorization": f"Bearer {SECRET}"})
        self.assertEqual(status, 429)
        # The upstream's length goes through; its own transfer coding does not.
        self.assertNotIn("transfer-encoding", headers)
        self.assertEqual(headers["content-length"], str(len(b'{"ok": true}')))
        self.assertEqual(body, b'{"ok": true}')

    def test_a_large_answer_is_streamed_in_pieces(self):
        # A git clone of a large repository: the proxy must never hold it whole.
        # It was killed for lack of memory, at 300 MB of a pack, in a 512 MB sidecar.
        FakeUpstream.response_body = bytes(range(256)) * (5 * sidecar.CHUNK // 256 + 7)
        writes = []
        real = sidecar.Proxy.relay

        def counting(handler, resp, *rest):
            w = handler.wfile.write
            handler.wfile.write = lambda b: writes.append(len(b)) or w(b)
            return real(handler, resp, *rest)
        sidecar.Proxy.relay = counting
        try:
            status, headers, body = request(sidecar.Proxy, "agent", "GET", "/github/o/r.git/info/refs",
                                            {"Authorization": f"Bearer {SECRET}"})
        finally:
            sidecar.Proxy.relay = real
        self.assertEqual(status, 200)
        self.assertEqual(body, FakeUpstream.response_body)
        self.assertEqual(headers.get("transfer-encoding"), "chunked")
        self.assertGreaterEqual(len(writes), 6)
        self.assertLessEqual(max(writes), sidecar.CHUNK + 32)

    def test_a_chunked_upload_is_streamed_upstream(self):
        # git push sends a large pack as a chunked request.
        payload = b"x" * (3 * sidecar.CHUNK + 5)
        chunked = b"".join(b"%x\r\n%s\r\n" % (len(p), p)
                           for p in (payload[:70000], payload[70000:])) + b"0\r\n\r\n"
        status, _, _ = request(sidecar.Proxy, "agent", "POST", "/github/o/r.git/git-receive-pack",
                               {"Authorization": f"Bearer {SECRET}", "Transfer-Encoding": "chunked"}, chunked)
        self.assertEqual(status, 200)
        [call] = FakeUpstream.calls
        self.assertEqual(call["body"], payload)
        self.assertTrue(call["chunked"])
        self.assertEqual(call["headers"].get("Transfer-Encoding"), "chunked")


def token_of(call: dict) -> str | None:
    """The token that this upstream call carries; None for no credential."""
    auth = call["headers"].get("Authorization")
    return base64.b64decode(auth[6:]).decode().partition(":")[2] if auth else None


class SeveralGitTokensTest(Base):
    """One sandbox, several tokens. A request names no token: the proxy tries."""

    UPSTREAM = "http://203.0.113.10:9001"
    PATH = "/git/203.0.113.10:9001/me/app"

    def setUp(self):
        super().setUp()
        self.set_tokens("claude=\ngithub=\n"
                        f"git.first={self.UPSTREAM} x-access-token tok-first\n"
                        f"git.second={self.UPSTREAM} oauth2 tok-second\n"
                        "git.gitlab=https://gitlab.example x-access-token tok-other-host\n")

    def get(self, service="git-upload-pack", path=None):
        return request(sidecar.Proxy, "agent", "GET", f"{path or self.PATH}.git/info/refs?service={service}",
                       {"x-api-key": SECRET})

    def post(self, service="git-upload-pack", path=None):
        return request(sidecar.Proxy, "agent", "POST", f"{path or self.PATH}.git/{service}",
                       {"x-api-key": SECRET}, b"want refs")

    def test_each_token_is_tried_until_one_works(self):
        FakeUpstream.status_for = lambda call: 200 if token_of(call) == "tok-second" else 404
        status, _, _ = self.get()
        self.assertEqual(status, 200)
        self.assertEqual([token_of(c) for c in FakeUpstream.calls], ["tok-first", "tok-second"])
        self.assertEqual(FakeUpstream.calls[1]["path"], "/me/app.git/info/refs?service=git-upload-pack")

    def test_the_token_that_worked_serves_the_request_that_follows(self):
        """The POST carries a body, so it cannot be tried twice."""
        FakeUpstream.status_for = lambda call: 200 if token_of(call) == "tok-second" else 404
        self.get()
        FakeUpstream.calls.clear()
        status, _, _ = self.post()
        self.assertEqual(status, 200)
        [call] = FakeUpstream.calls
        self.assertEqual(token_of(call), "tok-second")
        self.assertEqual(call["body"], b"want refs")

    def test_a_token_that_only_reads_does_not_stop_a_push(self):
        def host(call):
            if "receive-pack" in call["path"]:
                return 200 if token_of(call) == "tok-second" else 403
            return 200
        FakeUpstream.status_for = host
        self.get()  # a fetch: the first token works
        self.assertEqual(token_of(FakeUpstream.calls[-1]), "tok-first")
        FakeUpstream.calls.clear()
        self.assertEqual(self.get("git-receive-pack")[0], 200)
        self.assertEqual([token_of(c) for c in FakeUpstream.calls], ["tok-first", "tok-second"])
        FakeUpstream.calls.clear()
        self.post("git-receive-pack")
        self.assertEqual(token_of(FakeUpstream.calls[0]), "tok-second")
        FakeUpstream.calls.clear()
        self.post("git-upload-pack")
        self.assertEqual(token_of(FakeUpstream.calls[0]), "tok-first")

    def test_each_repository_finds_its_own_token(self):
        repos = {"/me/a": "tok-first", "/me/b": "tok-second"}
        FakeUpstream.status_for = lambda call: 200 if token_of(call) == repos[call["path"].split(".git")[0]] else 404
        prefix = "/git/203.0.113.10:9001"
        self.get(path=f"{prefix}/me/a")
        self.get(path=f"{prefix}/me/b")
        FakeUpstream.calls.clear()
        self.post(path=f"{prefix}/me/a")
        self.post(path=f"{prefix}/me/b")
        self.assertEqual([token_of(c) for c in FakeUpstream.calls], ["tok-first", "tok-second"])

    def test_a_public_repository_works_with_no_token_that_reaches_it(self):
        """A token that the host refuses must not hide a public repository."""
        FakeUpstream.status_for = lambda call: 200 if token_of(call) is None else 401
        self.assertEqual(self.get()[0], 200)
        self.assertEqual([token_of(c) for c in FakeUpstream.calls], ["tok-first", "tok-second", None])
        FakeUpstream.calls.clear()
        self.post()
        self.assertIsNone(token_of(FakeUpstream.calls[0]))

    def test_a_refusal_is_not_remembered(self):
        """A token added or fixed later works at the next git command."""
        FakeUpstream.status_for = lambda call: 404
        self.assertEqual(self.get()[0], 404)
        self.assertEqual(sidecar.CFG.git_choices, {})

    def test_a_token_never_goes_to_another_host(self):
        FakeUpstream.status_for = lambda call: 404
        self.get()
        sent = [token_of(c) for c in FakeUpstream.calls]
        self.assertNotIn("tok-other-host", sent)

    def test_a_host_with_no_token_is_refused_with_no_upstream_call(self):
        """A controlled pair with the tests above: this proxy is not a way out."""
        status, _, _ = request(sidecar.Proxy, "agent", "GET", "/git/evil.example/me/app.git/info/refs",
                               {"x-api-key": SECRET})
        self.assertEqual(status, 403)
        self.assertEqual(FakeUpstream.calls, [])

    def test_a_final_401_reaches_git_as_a_403(self):
        """git deletes a credential that gets a 401, here the placeholder, and
        every later git command then fails too."""
        FakeUpstream.status_for = lambda call: 401
        FakeUpstream.headers = [("WWW-Authenticate", 'Basic realm="GitHub"')]
        status, headers, _ = self.get()
        self.assertEqual(status, 403)
        self.assertNotIn("www-authenticate", headers)

    def test_the_proxys_own_401_stays_a_401(self):
        """git sends the placeholder only after a 401 that names a scheme."""
        status, headers, _ = request(sidecar.Proxy, "agent", "GET", f"{self.PATH}.git/info/refs")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["www-authenticate"])
        self.assertEqual(FakeUpstream.calls, [])

    def test_a_redirect_stays_under_the_prefix_it_came_in_on(self):
        FakeUpstream.status = 301
        FakeUpstream.headers = [("Location", f"{self.UPSTREAM}/me/app.git/info/refs?service=git-upload-pack")]
        _, headers, _ = request(sidecar.Proxy, "agent", "GET", f"{self.PATH}/info/refs?service=git-upload-pack",
                                {"x-api-key": SECRET})
        self.assertEqual(headers["location"], f"{self.PATH}.git/info/refs?service=git-upload-pack")

    def test_the_log_names_each_token_and_shows_no_value(self):
        FakeUpstream.status_for = lambda call: 200 if token_of(call) == "tok-second" else 404
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.get()
        self.assertIn("(first) -> 404", err.getvalue())
        self.assertIn("(second) -> 200", err.getvalue())
        self.assertNotIn("tok-", err.getvalue())

    def test_a_project_token_and_a_named_token_work_together(self):
        self.set_tokens(f"claude=\ngithub=tok-project\ngit_user=\ngit.extra={self.UPSTREAM} x-access-token tok-extra\n")
        FakeUpstream.status_for = lambda call: 200 if token_of(call) == "tok-extra" else 404
        status, _, _ = request(sidecar.Proxy, "agent", "GET", "/github/me/other.git/info/refs?service=git-upload-pack",
                               {"x-api-key": SECRET})
        self.assertEqual(status, 200)
        self.assertEqual([token_of(c) for c in FakeUpstream.calls], ["tok-project", "tok-extra"])

    def test_a_bad_line_is_ignored_and_its_value_is_not_logged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.set_tokens("claude=\ngit.bad=only-a-secret-value\n")
        self.assertEqual(sidecar.CFG.named_git, [])
        self.assertIn("git.bad", err.getvalue())
        self.assertNotIn("only-a-secret-value", err.getvalue())


class RealGitTest(unittest.TestCase):
    """The real git program against the real proxy, on the loopback. The claim:
    a repository that no token reaches fails, and the next one still works."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        (d / "secret").write_text(SECRET + "\n")
        self.hits: list[tuple[str, str | None]] = []
        upstream = self.start(self.fake_host_handler())
        self.host = f"127.0.0.1:{upstream.server_address[1]}"
        (d / "tokens").write_text(f"claude=\ngit.first=http://{self.host} x-access-token tok-a\n"
                                  f"git.second=http://{self.host} x-access-token tok-b\n")
        sidecar.CFG = sidecar.Config(types.SimpleNamespace(
            agent_addr="127.0.0.1", vm_addr="127.0.0.1", net_addr="127.0.0.1",
            secret_file=str(d / "secret"), tokens_file=str(d / "tokens"),
            claude_upstream=f"http://{self.host}", github_upstream=f"http://{self.host}", net_expose_port=8081))
        proxy = self.start(sidecar.Proxy)
        self.proxy = f"http://127.0.0.1:{proxy.server_address[1]}"
        self.home = d / "home"
        self.home.mkdir()
        self.creds = self.home / ".git-credentials"
        self.creds.write_text(f"http://sbx:{SECRET}@127.0.0.1:{proxy.server_address[1]}\n")
        self.env = {"HOME": str(self.home), "PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("config", "--global", "credential.helper", "store")
        self.git("config", "--global", f"url.{self.proxy}/git/{self.host}/.insteadOf", "https://github.example/")

    def start(self, handler):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], env=self.env, capture_output=True, text=True, timeout=30)

    def fake_host_handler(self):
        """A git host: /me/a answers to tok-a, /me/b to tok-b, /me/public to
        anyone, and anything else, or no credential, gets GitHub's 401."""
        test = self
        owners = {"/me/a": "tok-a", "/me/b": "tok-b"}

        class Host(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                auth = self.headers.get("Authorization")
                token = base64.b64decode(auth[6:]).decode().partition(":")[2] if auth else None
                repo = self.path.split("/info/refs")[0].removesuffix(".git")
                test.hits.append((repo, token))
                if repo != "/me/public" and (token is None or owners.get(repo) != token):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="GitHub"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                sha = "1" * 40
                line = f"{sha} HEAD\0agent=fake\n"
                data = (b"001e# service=git-upload-pack\n0000"
                        + f"{len(line) + 4:04x}{line}".encode() + b"0000")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        return Host

    def ls_remote(self, repo: str) -> subprocess.CompletedProcess:
        return self.git("ls-remote", f"https://github.example/{repo}")

    def test_git_reaches_each_repository_with_the_token_that_covers_it(self):
        self.assertEqual(self.ls_remote("me/a").returncode, 0)
        done = self.ls_remote("me/b")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("1" * 40, done.stdout)
        self.assertIn(("/me/b", "tok-b"), self.hits)

    def test_a_repository_that_no_token_reaches_breaks_nothing_after_it(self):
        failed = self.ls_remote("me/private")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(SECRET, self.creds.read_text(), "git must not delete the placeholder after a refusal")
        self.assertNotIn(SECRET, failed.stderr, "git's error shows no placeholder")
        done = self.ls_remote("me/b")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_public_repository_needs_no_token_that_reaches_it(self):
        done = self.ls_remote("me/public")
        self.assertEqual(done.returncode, 0, done.stderr)


class ExposeTest(Base):
    def ask(self, port=4400, side="agent", headers=None):
        return request(sidecar.Expose, side, "POST", "/expose",
                       {"Authorization": f"Bearer {SECRET}"} if headers is None else headers,
                       json.dumps({"port": port}).encode())

    def decide(self, what, port=4400, side="net"):
        return request(sidecar.Expose, side, "POST", f"/{what}", {}, json.dumps({"port": port}).encode())

    def states(self, side="net"):
        _, _, body = request(sidecar.Expose, side, "GET", "/requests")
        return {r["port"]: r["state"] for r in json.loads(body)["requests"]}

    def test_the_sandbox_asks_and_only_the_trusted_side_opens_the_port(self):
        status, _, body = self.ask()
        self.assertEqual((status, json.loads(body)), (202, {"port": 4400, "state": "pending"}))
        self.assertEqual(self.states(), {4400: "pending"})
        self.assertEqual(self.nft, [], "nothing opens before an approval")

        status, _, _ = self.decide("approve", side="agent")
        self.assertEqual(status, 403)
        self.assertEqual(self.nft, [], "the sandbox side cannot open a port")

        status, _, body = self.decide("approve")
        self.assertEqual((status, json.loads(body)["state"]), (200, "approved"))
        self.assertEqual(self.nft, [("add", 4400)])
        self.assertEqual(self.states(side="agent"), {4400: "approved"})

        status, _, _ = self.decide("approve")
        self.assertEqual(status, 200)
        self.assertEqual(self.nft, [("add", 4400)], "a second approval adds nothing")

        status, _, body = self.decide("deny")
        self.assertEqual((status, json.loads(body)["state"]), (200, "denied"))
        self.assertEqual(self.nft, [("add", 4400), ("delete", 4400)])

        status, _, _ = self.decide("deny")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.nft), 2, "a denial of a port that is not open deletes nothing")

    def test_approvals_survive_a_restart_and_a_denial_is_kept_too(self):
        path = Path(self.tmp.name) / "approved"
        sidecar.REQUESTS = sidecar.Requests(str(path))
        self.ask()
        self.decide("approve")
        self.assertEqual(path.read_text(), "4400\n")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        # A restart (the apply script renders an empty firewall, then restarts
        # the service): a fresh Requests opens the port again.
        self.nft.clear()
        sidecar.REQUESTS = sidecar.Requests(str(path))
        self.assertEqual(sidecar.REQUESTS.restore(), [4400])
        self.assertEqual(self.nft, [("add", 4400)])
        self.assertEqual(self.states(), {4400: "approved"})
        self.decide("deny")
        self.assertEqual(path.read_text(), "")
        self.assertEqual(sidecar.Requests(str(path)).restore(), [])

    def test_restore_rechecks_the_file(self):
        # The file is input: only ports of the range, never the sidecar's own.
        path = Path(self.tmp.name) / "approved"
        path.write_text("4400\n22\n8081\n2222\n70000\nx; rm -rf /\n5173\n")
        self.assertEqual(sidecar.Requests(str(path)).restore(), [4400, 5173])
        self.assertEqual(self.nft, [("add", 4400), ("add", 5173)])

    def test_a_missing_file_restores_nothing(self):
        self.assertEqual(sidecar.Requests(str(Path(self.tmp.name) / "none")).restore(), [])
        self.assertEqual(sidecar.Requests("").restore(), [])

    def test_a_refused_firewall_change_is_an_error_not_an_approval(self):
        self.ask()
        self.nft_ok = False
        status, _, body = self.decide("approve")
        self.assertEqual((status, json.loads(body)["state"]), (500, "error"))
        self.assertEqual(self.states(), {4400: "pending"})

    def test_a_request_needs_the_placeholder_and_a_sane_port(self):
        status, _, _ = self.ask(headers={})
        self.assertEqual(status, 401)
        status, _, _ = self.ask(headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        for port in (22, 0, 65000, True, "4400"):
            status, _, _ = request(sidecar.Expose, "agent", "POST", "/expose",
                                   {"Authorization": f"Bearer {SECRET}"}, json.dumps({"port": port}).encode())
            self.assertEqual(status, 400, port)
        status, _, _ = request(sidecar.Expose, "agent", "POST", "/expose",
                               {"Authorization": f"Bearer {SECRET}"}, b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(self.states(), {})

    def test_the_trusted_side_cannot_ask_on_the_sandboxs_behalf(self):
        status, _, _ = self.ask(side="net", headers={})
        self.assertEqual(status, 403)
        status, _, _ = self.decide("approve")
        self.assertEqual(status, 404, "nothing was asked for")
        status, _, _ = request(sidecar.Expose, "net", "GET", "/nope")
        self.assertEqual(status, 404)


class ConfigTest(Base):
    def test_an_empty_secret_is_refused(self):
        Path(self.args.secret_file).write_text("\n")
        with self.assertRaises(SystemExit):
            sidecar.Config(self.args)

    def test_missing_tokens_are_empty_not_errors(self):
        Path(self.args.tokens_file).write_text("# nothing yet\n")
        cfg = sidecar.Config(self.args)
        self.assertEqual(cfg.tokens, {"claude": "", "github": "", "git_user": ""})


if __name__ == "__main__":
    unittest.main()
