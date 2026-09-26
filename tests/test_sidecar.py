"""sidecar/sidecar.py, driven without a network: each handler gets an
in-memory socket, the upstream is a fake connection class, and the nftables
calls are stubs. tests/run-sidecar-test.sh proves the same flows on real
interfaces; this file proves the handlers' decisions."""
from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
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
    def __init__(self, status: int, body: bytes):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def getheaders(self):
        return [("Content-Type", "application/json"), ("Transfer-Encoding", "chunked")]


class FakeUpstream:
    """Stands in for http.client.HTTPConnection. Records the one request."""
    calls: list[dict] = []
    status = 200

    def __init__(self, host, port=None, timeout=None):
        self.host, self.port = host, port

    def request(self, method, path, body=None, headers=None):
        FakeUpstream.calls.append({"host": self.host, "port": self.port, "method": method,
                                   "path": path, "body": body, "headers": dict(headers or {})})

    def getresponse(self):
        return FakeResponse(FakeUpstream.status, b'{"ok": true}')

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
    return status, out_headers, out_body


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
        FakeUpstream.status = 200
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

    def test_wrong_or_missing_placeholder_is_refused_before_any_upstream_call(self):
        wrong_basic = base64.b64encode(b"sbx:wrong").decode()
        for headers in ({"Authorization": "Bearer wrong"}, {"x-api-key": "wrong"},
                        {"Authorization": f"Basic {wrong_basic}"}, {"Authorization": "Basic %%%"}, {}):
            status, _, body = request(sidecar.Proxy, "agent", "GET", "/v1/models", headers)
            self.assertEqual(status, 401, headers)
            self.assertIn("unknown sandbox credential", body.decode())
        self.assertEqual(FakeUpstream.calls, [])

    def test_upstream_status_and_hop_headers(self):
        FakeUpstream.status = 429
        status, headers, _ = request(sidecar.Proxy, "agent", "GET", "/v1/models",
                                     {"Authorization": f"Bearer {SECRET}"})
        self.assertEqual(status, 429)
        self.assertNotIn("transfer-encoding", headers)
        self.assertEqual(headers["content-length"], str(len(b'{"ok": true}')))


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
        self.assertEqual(cfg.tokens, {"claude": "", "github": ""})


if __name__ == "__main__":
    unittest.main()
