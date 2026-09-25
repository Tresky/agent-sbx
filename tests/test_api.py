"""HttpApi against a REAL local HTTPS server: the header, the encoding, the two
ways to verify the self-signed certificate, and the task wait."""
import hashlib
import http.server
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from sbxlib.config import Config, ConfigError
from sbxlib.pve import HttpApi, Pve, PveError
from sbxlib.run import Runner

TOKEN = "sbx@pve!cli=12345678-1234-1234-1234-123456789abc"
PUBKEY = "ssh-ed25519 AAAAC3Nza+/= sbx key"


class Handler(http.server.BaseHTTPRequestHandler):
    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        self.server.seen.append({"method": self.command, "path": self.path, "body": body,
                                 "auth": self.headers.get("Authorization")})
        status, data = self.server.script(self.command, self.path)
        out = json.dumps({"data": data} if status < 400 else {"message": data}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_DELETE = _reply

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cert, key = tmp / "cert.pem", tmp / "key.pem"
        done = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                               "-subj", "/CN=pve-test", "-addext", "subjectAltName=IP:127.0.0.1",
                               "-keyout", str(key), "-out", str(cls.cert)], capture_output=True)
        if done.returncode != 0:
            raise unittest.SkipTest("this openssl cannot make a SAN certificate")
        der = ssl.PEM_cert_to_DER_cert(cls.cert.read_text())
        digest = hashlib.sha256(der).hexdigest().upper()
        cls.fingerprint = ":".join(digest[i:i + 2] for i in range(0, 64, 2))

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cls.cert, key)
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.server.seen, cls.server.script = [], lambda m, p: (200, None)
        # A client that refuses the certificate resets the connection on
        # purpose; the server's traceback for that is noise, not a failure.
        cls.server.handle_error = lambda *args: None
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"https://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.server.seen.clear()
        self.server.script = lambda m, p: (200, None)

    def api(self, **kw):
        cfg = Config(pve_api=self.url, pve_token_command=["print-token"], **kw)
        return HttpApi(cfg, Runner(responder=lambda argv, data: TOKEN + "\n")), cfg

    def test_ca_file_verifies_and_the_token_header_is_right(self):
        api, _ = self.api(pve_ca_file=str(self.cert))
        self.server.script = lambda m, p: (200, [{"vmid": 1}])
        self.assertEqual(api("GET", "/cluster/resources", {"type": "vm"}), [{"vmid": 1}])
        seen = self.server.seen[0]
        self.assertEqual(seen["auth"], f"PVEAPIToken={TOKEN}")
        self.assertEqual(seen["path"], "/api2/json/cluster/resources?type=vm")

    def test_a_wrong_ca_refuses_the_connection(self):
        other = Path(self.tmp.name) / "other.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=x",
                        "-keyout", str(other) + ".key", "-out", str(other)], capture_output=True, check=True)
        api, _ = self.api(pve_ca_file=str(other))
        with self.assertRaises(PveError):
            api("GET", "/version")
        self.assertEqual(self.server.seen, [], "the token reached a server that failed verification")

    def test_fingerprint_pin_controlled_pair(self):
        good, _ = self.api(pve_fingerprint=self.fingerprint)
        good("GET", "/version")
        self.assertEqual(len(self.server.seen), 1)

        # Same server, same request; only the pin differs. Nothing may arrive:
        # the check runs before the request, so the token is never sent.
        self.server.seen.clear()
        bad, _ = self.api(pve_fingerprint="00:" * 31 + "00")
        with self.assertRaises(PveError) as ctx:
            bad("GET", "/version")
        self.assertIn(self.fingerprint, str(ctx.exception))
        self.assertEqual(self.server.seen, [])

    def test_no_verification_method_is_refused(self):
        with self.assertRaises(ConfigError):
            self.api()
        with self.assertRaises(ConfigError):
            HttpApi(Config(pve_api="http://127.0.0.1:8006", pve_token_command=["x"], pve_fingerprint="00"), Runner())

    def test_a_malformed_token_is_refused_before_any_request(self):
        cfg = Config(pve_api=self.url, pve_token_command=["x"], pve_fingerprint=self.fingerprint)
        api = HttpApi(cfg, Runner(responder=lambda argv, data: "root@pam:password"))
        with self.assertRaises(ConfigError):
            api("GET", "/version")
        self.assertEqual(self.server.seen, [])

    def test_403_names_the_permission_script(self):
        api, _ = self.api(pve_fingerprint=self.fingerprint)
        self.server.script = lambda m, p: (403, "Permission check failed (/vms/100, VM.Audit)")
        with self.assertRaises(PveError) as ctx:
            api("GET", "/nodes/pve/qemu/100/config")
        self.assertIn("40-api-token.sh", str(ctx.exception))
        self.assertIn("VM.Audit", str(ctx.exception))

    def test_create_encodes_sshkeys_twice_and_waits_for_the_clone_task(self):
        api, cfg = self.api(pve_fingerprint=self.fingerprint)
        upid = "UPID:pve:0001:0002:0003:qmclone:9101:sbx@pve!cli:"
        polls = []

        def script(method, path):
            if path.startswith("/api2/json/cluster/resources"):
                return 200, [{"vmid": 9000, "node": "pve", "type": "qemu", "template": 1}]
            if path.endswith("/clone"):
                return 200, upid
            if "/tasks/" in path:
                polls.append(path)
                return 200, ({"status": "running"} if len(polls) < 2 else {"status": "stopped", "exitstatus": "OK"})
            return 200, None
        self.server.script = script

        with mock.patch("sbxlib.pve.time.sleep"):
            node = Pve(cfg, api).create(9101, "sbx-myapp", "agent", PUBKEY, cores=4, memory_mb=8192,
                                        disk_gb=None, expires=None)
        self.assertEqual(node, "pve")
        self.assertEqual(len(polls), 2, "create must block until the clone task has stopped")
        self.assertIn(urllib.parse.quote(upid, safe=""), polls[0])  # a raw UPID has colons: not a legal path

        order = [(s["method"], s["path"].rsplit("/", 1)[-1].split("?")[0]) for s in self.server.seen]
        self.assertLess(order.index(("POST", "clone")), order.index(("PUT", "config")))
        clone = urllib.parse.parse_qs(next(s for s in self.server.seen if s["path"].endswith("/clone"))["body"])
        self.assertEqual((clone["newid"], clone["name"], clone["pool"]), (["9101"], ["sbx-myapp"], ["sbx"]))
        config = urllib.parse.parse_qs(next(s for s in self.server.seen if s["path"].endswith("/config"))["body"])
        # After the form decode, the key is STILL URL-encoded: that is the form Proxmox expects.
        self.assertEqual(config["sshkeys"], [urllib.parse.quote(PUBKEY, safe="")])
        self.assertEqual(config["net0"], ["virtio,bridge=vmbr77"])
        self.assertEqual(config["tags"], ["sbx;sbx-agent"])

    def test_a_failed_task_is_an_error(self):
        api, cfg = self.api(pve_fingerprint=self.fingerprint)
        self.server.script = lambda m, p: (200, {"status": "stopped", "exitstatus": "clone failed: no space"}
                                           if "/tasks/" in p else "UPID:pve:1:2:3:qmstart:9101:u:")
        with self.assertRaises(PveError) as ctx:
            Pve(cfg, api).start("pve", 9101)
        self.assertIn("no space", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
