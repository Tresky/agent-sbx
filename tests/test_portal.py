"""`sbx web`: the portal's locks, its routes against a fake Proxmox API, and
the rule that no secret reaches a job's record or a reply."""
import datetime as dt
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sbxlib.portal import api, server
from sbxlib.portal.jobs import Jobs
from sbxlib.run import Result, Runner

TOKEN = "portal-test-token"
SECRET = "ghp_s3cretTokenValue"


class FakeApi:
    def __init__(self):
        self.calls = []
        self.resources = [
            {"type": "qemu", "vmid": 9000, "name": "sbx-tpl-rails-1", "node": "pve", "template": 1,
             "tags": "sbx-template;sbx-tpl-rails;sbx-h-abc"},
            {"type": "qemu", "vmid": 9101, "name": "sbx-lab", "node": "pve", "status": "running",
             "tags": "sbx;sbx-agent;sbx-tpl-rails;sbx-exp-20200101", "cpu": 0.25, "maxcpu": 8,
             "mem": 1 << 30, "maxmem": 4 << 30, "uptime": 90},
        ]

    def __call__(self, method, path, params=None):
        self.calls.append((method, path, dict(params or {})))
        if path == "/cluster/resources":
            return self.resources
        if path == "/version":
            return {"version": "9.0"}
        if path.endswith("/snapshot"):
            return [{"name": "clean", "snaptime": 1700000000}, {"name": "current"}]
        if path.endswith("/config"):
            return {"cores": 8, "memory": 4096}
        if path.endswith("/agent/network-get-interfaces"):
            return {"result": []}
        if "/tasks/" in path:
            return {"status": "stopped", "exitstatus": "OK"}
        if method in ("POST", "DELETE"):
            return "UPID:pve:1:2:3:task:9101:sbx@pve!cli:"
        return None


class PortalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "config.toml").write_text('pve_api = "https://192.168.1.10:8006"\n'
                                               'pve_token_command = ["true"]\npve_fingerprint = "00"\n')
        self.env = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(self.home)})
        self.env.start()
        self.commands = []

        def responder(argv, data):
            self.commands.append(list(argv))
            if argv[:2] == ["security", "find-generic-password"]:
                return Result(44)
            return ""

        # A fake `sbx`: it prints its arguments and the length of its stdin.
        echo = [sys.executable, "-c",
                "import sys; data = sys.stdin.read(); print('ARGS', *sys.argv[1:]); print('STDIN', len(data))"]
        self.jobs = Jobs(self.home / "portal" / "jobs", sbx_argv=echo)
        self.fake = FakeApi()
        self.ctx = api.Context(self.jobs, Runner(responder=responder), self.fake)
        self.server = server.Portal(0, self.ctx, token=TOKEN)
        self.port = self.server.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        self._tmp.cleanup()

    def request(self, method, path, body=None, *, cookie=True, host=None, origin=True, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if cookie:
            headers["Cookie"] = f"sbx_portal_{self.port}={TOKEN}"
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        data = None
        if method != "GET":
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = ctype
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw.decode(errors="replace")
        return resp.status, payload, resp

    def wait_job(self, job_id):
        for _ in range(100):
            status, j, _ = self.request("GET", f"/api/jobs/{job_id}")
            if j["status"] != "running":
                return j
            time.sleep(0.05)
        self.fail("the job did not end")

    # --- the locks ---
    def test_no_cookie_is_refused(self):
        status, body, _ = self.request("GET", "/api/sandboxes", cookie=False)
        self.assertEqual(status, 401)

    def test_a_foreign_host_name_is_refused(self):
        # DNS rebinding: a hostile name that resolves to 127.0.0.1.
        status, _, _ = self.request("GET", "/api/sandboxes", host=f"evil.example:{self.port}")
        self.assertEqual(status, 403)

    def test_the_link_sets_the_cookie_and_a_wrong_token_does_not(self):
        status, _, resp = self.request("GET", f"/?t={TOKEN}", cookie=False)
        self.assertEqual(status, 200)
        cookie = resp.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        status, _, resp = self.request("GET", "/?t=wrong", cookie=False)
        self.assertEqual(status, 403)
        self.assertIsNone(resp.getheader("Set-Cookie"))

    def test_a_change_needs_the_portal_origin_and_json(self):
        status, _, _ = self.request("POST", "/api/gc", origin=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/gc", ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 415)
        self.assertEqual(self.jobs.all(), [])

    def test_the_page_forbids_outside_code(self):
        status, _, resp = self.request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", resp.getheader("Content-Security-Policy"))
        status, _, _ = self.request("GET", "/static/../server.py")
        self.assertEqual(status, 404)

    # --- reads ---
    def test_the_sandbox_list(self):
        status, body, _ = self.request("GET", "/api/sandboxes")
        self.assertEqual(status, 200)
        (box,) = body["sandboxes"]
        self.assertEqual((box["name"], box["profile"], box["template"], box["expired"]), ("lab", "agent", "rails", True))
        self.assertEqual(box["cpu"], 0.25)

    def test_one_sandbox_hides_the_current_pseudo_snapshot(self):
        status, body, _ = self.request("GET", "/api/sandboxes/lab")
        self.assertEqual(status, 200)
        self.assertEqual([s["name"] for s in body["snapshots"]], ["clean"])
        status, _, _ = self.request("GET", "/api/sandboxes/nope")
        self.assertEqual(status, 404)

    def test_a_log_is_one_of_a_fixed_list(self):
        status, _, _ = self.request("GET", "/api/sandboxes/lab/logs/etc-shadow")
        self.assertEqual(status, 404)

    def test_the_overview_reads_no_secret(self):
        status, body, _ = self.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(body["pve_version"], "9.0")
        for argv in self.commands:
            if argv[:2] == ["security", "find-generic-password"]:
                self.assertNotIn("-w", argv)

    def test_every_doc_opens(self):
        status, body, _ = self.request("GET", "/api/docs")
        self.assertEqual(status, 200)
        for doc in body["docs"]:
            with self.subTest(doc=doc["name"]):
                status, got, _ = self.request("GET", f"/api/docs/{doc['name']}")
                self.assertEqual(status, 200)
                self.assertTrue(got["text"].startswith("#"))

    # --- changes ---
    def test_the_expiry_keeps_the_other_tags(self):
        status, body, _ = self.request("POST", "/api/sandboxes/lab/expiry", {"days": 5})
        self.assertEqual(status, 200)
        (put,) = [c for c in self.fake.calls if c[0] == "PUT"]
        want = f"sbx-exp-{dt.date.today() + dt.timedelta(days=5):%Y%m%d}"
        self.assertEqual(put[2]["tags"], f"sbx;sbx-agent;sbx-tpl-rails;{want}")

    def test_new_runs_sbx_new_with_each_value_after_an_equals_sign(self):
        status, body, _ = self.request("POST", "/api/sandboxes", {
            "name": "app", "profile": "agent", "project": "-rf", "with": ["key"], "without": ["STRIPE"], "ttl": 0})
        self.assertEqual(status, 200, body)
        j = self.wait_job(body["job"]["id"])
        self.assertIn("ARGS new app --profile=agent --project=-rf --with=key --without=STRIPE --ttl=0", j["lines"])

    def test_new_refuses_a_bad_name_before_a_job(self):
        status, body, _ = self.request("POST", "/api/sandboxes", {"name": "Not A Name"})
        self.assertEqual(status, 400)
        self.assertEqual(self.jobs.all(), [])

    def test_a_secret_goes_to_stdin_and_nowhere_else(self):
        (self.home / "projects.toml").write_text('[projects."app"]\nurl = "git@github.com:me/app.git"\n')
        status, body, _ = self.request("POST", "/api/projects/app/git-token", {"token": SECRET})
        self.assertEqual(status, 200, body)
        j = self.wait_job(body["job"]["id"])
        self.assertIn(f"STDIN {len(SECRET) + 1}", j["lines"])
        self.assertNotIn(SECRET, json.dumps(j))
        for record in (self.home / "portal" / "jobs").glob("*.json"):
            self.assertNotIn(SECRET, record.read_text())

    def test_push_sends_the_stored_token_with_no_secret(self):
        (self.home / "projects.toml").write_text('[projects."app"]\nurl = "git@github.com:me/app.git"\n')
        status, body, _ = self.request("POST", "/api/projects/app/git-token/push", {"sandboxes": ["lab", "sbx-api"]})
        self.assertEqual(status, 200, body)
        j = self.wait_job(body["job"]["id"])
        self.assertIn("ARGS git-token --push=lab --push=api -- app", j["lines"])
        self.assertIn("STDIN 0", j["lines"])
        status, _, _ = self.request("POST", "/api/projects/app/git-token/push", {"sandboxes": ["../x"]})
        self.assertEqual(status, 400)

    def test_one_job_at_a_time_on_one_sandbox(self):
        slow = Jobs(self.home / "slow", sbx_argv=[sys.executable, "-c", "import time; time.sleep(2)"])
        self.ctx.jobs = slow
        status, body, _ = self.request("DELETE", "/api/sandboxes/lab")
        self.assertEqual(status, 200)
        status, second, _ = self.request("POST", "/api/sandboxes/lab/snapshots", {"label": "before"})
        self.assertEqual(status, 409)
        self.assertTrue(slow.cancel(body["job"]["id"]))

    def test_settings_refuse_a_shared_key_and_keep_the_file_on_an_error(self):
        before = (self.home / "config.toml").read_text()
        status, body, _ = self.request("PUT", "/api/settings", {"values": {"domain": "x.internal"}})
        self.assertEqual(status, 400)
        status, body, _ = self.request("PUT", "/api/settings", {"values": {"default_profile": "root"}})
        self.assertEqual(status, 400)
        self.assertEqual((self.home / "config.toml").read_text(), before)
        status, body, _ = self.request("PUT", "/api/settings", {"values": {"cores": 4}})
        self.assertEqual(status, 200, body)
        self.assertIn("cores = 4", (self.home / "config.toml").read_text())
        status, body, _ = self.request("PUT", "/api/settings", {"values": {"cores": None}})
        self.assertNotIn("cores", (self.home / "config.toml").read_text())

    def test_an_import_writes_only_what_was_reviewed(self):
        bundle = ('[sbx_template]\nformat = 1\nname = "portaltest"\n'
                  "definition = '''\ndescription = \"t\"\ncomponents = []\n'''\n")
        status, plan, _ = self.request("POST", "/api/templates/import", {"text": bundle})
        self.assertEqual(status, 200, plan)
        self.assertEqual([w["path"] for w in plan["writes"]], ["templates/local/portaltest.toml"])
        status, _, _ = self.request("POST", "/api/templates/import", {"text": bundle, "apply": True, "digest": "0" * 64})
        self.assertEqual(status, 409)

    def test_terminal_opens_sbx_commands_only(self):
        status, _, _ = self.request("POST", "/api/terminal", {"args": ["rm", "lab", "-y"]})
        self.assertEqual(status, 400)
        status, body, _ = self.request("POST", "/api/terminal", {"args": ["ssh", "lab"]})
        self.assertEqual(status, 200, body)
        osa = next(c for c in self.commands if c[0] == "osascript")
        self.assertIn("bin/sbx ssh lab", osa[-1])


class JobsTest(unittest.TestCase):
    def test_a_job_that_the_portal_left_running_reads_back_as_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "0001-aa.json").write_text(json.dumps({"id": "0001-aa", "title": "t", "command": "sbx x",
                                                        "started": 1.0, "status": "running"}))
            self.assertEqual(Jobs(d).get("0001-aa").status, "interrupted")


if __name__ == "__main__":
    unittest.main()
