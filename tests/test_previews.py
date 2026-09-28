import itertools
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import previews
from sbxlib.config import Config, ConfigError

ACCT = "/accounts/acc1"


class FakeCloudflare:
    """The parts of the Cloudflare API that previews.py uses, in memory. Every
    call is recorded, so a test can check the order."""

    def __init__(self):
        self.calls = []
        self.ids = (f"id{n}" for n in itertools.count(1))
        self.zones = [{"id": "z1", "name": "sbx-previews.example"}]
        self.tunnels, self.configs, self.records, self.apps, self.policies = {}, {}, {}, {}, {}

    def __call__(self, method, path, body=None):
        self.calls.append((method, path.split("?")[0], body))
        base, _, query = path.partition("?")
        q = dict(p.split("=", 1) for p in query.split("&")) if query else {}
        parts = base.strip("/").split("/")
        if base == "/zones":
            return [z for z in self.zones if z["name"] == q.get("name")]
        if parts[:2] == ["zones", "z1"] and parts[2] == "dns_records":
            if len(parts) == 3 and method == "GET":
                return [r for r in self.records.values() if r["name"] == q.get("name")]
            if len(parts) == 3 and method == "POST":
                rid = next(self.ids)
                self.records[rid] = {"id": rid, **body}
                return self.records[rid]
            if method == "PUT":
                self.records[parts[3]].update(body)
                return self.records[parts[3]]
            if method == "DELETE":
                del self.records[parts[3]]
                return {}
        rest = base.removeprefix(ACCT + "/")
        if rest == "cfd_tunnel":
            if method == "POST":
                tid = next(self.ids)
                self.tunnels[tid] = {"id": tid, "name": body["name"]}
                return self.tunnels[tid]
            return [t for t in self.tunnels.values() if "name" not in q or t["name"] == q["name"]]
        if rest.startswith("cfd_tunnel/"):
            tid, _, what = rest.removeprefix("cfd_tunnel/").partition("/")
            if what == "configurations":
                if method == "PUT":
                    self.configs[tid] = body["config"]
                return {"config": self.configs.get(tid)}
            if what == "token":
                return f"token-of-{tid}"
            if what == "connections":
                return {}
            if method == "DELETE":
                del self.tunnels[tid]
                self.configs.pop(tid, None)
                return {}
        for kind, store in (("access/apps", self.apps), ("access/policies", self.policies)):
            if rest == kind:
                if method == "POST":
                    oid = next(self.ids)
                    store[oid] = {"id": oid, **body}
                    if kind == "access/apps":
                        store[oid]["policies"] = [{"id": p["id"], "name": self.policies[p["id"]]["name"]}
                                                  for p in body["policies"]]
                    return store[oid]
                return list(store.values())
            if rest.startswith(kind + "/"):
                oid = rest.rsplit("/", 1)[1]
                if method == "DELETE":
                    del store[oid]
                    return {}
                store[oid].update(body)
                if kind == "access/apps":
                    store[oid]["policies"] = [{"id": p["id"], "name": self.policies[p["id"]]["name"]}
                                              for p in body["policies"]]
                return store[oid]
        raise AssertionError(f"unexpected call {method} {path}")


def cfg():
    return Config(preview_zone="sbx-previews.example", cloudflare_account_id="acc1",
                  cloudflare_token_command=["true"])


POLICIES = {"me": previews.Policy("me", emails=["me@example.com"]),
            "team": previews.Policy("team", emails=["a@example.com"], email_domains=["example.org"])}


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.cf = FakeCloudflare()
        self.pv = previews.Previews(cfg(), self.cf)

    def publish(self, fqdn="lab-3000.sbx-previews.example", policy="me", host="sbx-lab"):
        return self.pv.publish(host, fqdn, "http://10.79.0.2:3000", POLICIES[policy], POLICIES)

    def test_access_comes_before_the_route_and_the_name(self):
        token, new = self.publish(policy="team")
        self.assertTrue(new)
        self.assertEqual(token, "token-of-" + next(iter(self.cf.tunnels)))
        order = [(m, p) for m, p, _ in self.cf.calls if m != "GET"]
        first_app = next(i for i, c in enumerate(order) if c == ("POST", ACCT + "/access/apps"))
        host_app = [i for i, c in enumerate(order) if c == ("POST", ACCT + "/access/apps")][1]
        route = next(i for i, c in enumerate(order) if c[1].endswith("/configurations"))
        record = next(i for i, c in enumerate(order) if c == ("POST", "/zones/z1/dns_records"))
        self.assertLess(first_app, host_app)
        self.assertLess(host_app, route)
        self.assertLess(route, record)
        wildcard = next(a for a in self.cf.apps.values() if a["domain"] == "*.sbx-previews.example")
        self.assertEqual(wildcard["session_duration"], "336h")

    def test_the_default_policy_needs_no_application_of_its_own(self):
        self.publish()
        self.assertEqual([a["domain"] for a in self.cf.apps.values()], ["*.sbx-previews.example"])
        self.assertEqual(self.pv.policy_of("lab-3000.sbx-previews.example"), "me")

    def test_a_named_policy_gets_an_application_and_back_to_me_removes_it(self):
        self.publish(policy="team")
        self.assertEqual(self.pv.policy_of("lab-3000.sbx-previews.example"), "team")
        pol = next(p for p in self.cf.policies.values() if p["name"] == "sbx: team")
        self.assertEqual(pol["decision"], "allow")
        self.assertEqual(pol["include"], [{"email": {"email": "a@example.com"}},
                                          {"email_domain": {"domain": "example.org"}}])
        self.publish(policy="me")
        self.assertEqual(self.pv.policy_of("lab-3000.sbx-previews.example"), "me")
        self.assertEqual(len(self.cf.apps), 1)

    def test_the_route_points_at_the_vm_and_ends_in_a_404(self):
        self.publish()
        self.publish(fqdn="lab-4000.sbx-previews.example")
        (conf,) = self.cf.configs.values()
        self.assertEqual(conf["ingress"][-1], {"service": "http_status:404"})
        self.assertEqual({r.get("hostname") for r in conf["ingress"][:-1]},
                         {"lab-3000.sbx-previews.example", "lab-4000.sbx-previews.example"})
        self.assertEqual(len(self.cf.tunnels), 1, "one tunnel per sandbox")
        (rec,) = [r for r in self.cf.records.values() if r["name"].startswith("lab-3000")]
        self.assertTrue(rec["proxied"])
        self.assertEqual(rec["content"], f"{next(iter(self.cf.tunnels))}.cfargotunnel.com")

    def test_a_tls_origin_skips_verification_on_the_wire_only(self):
        self.pv.publish("sbx-lab", "lab-3000.sbx-previews.example", "https://10.79.0.2:3000",
                        POLICIES["me"], POLICIES, tls=True)
        (conf,) = self.cf.configs.values()
        self.assertEqual(conf["ingress"][0]["originRequest"], {"noTLSVerify": True})
        self.assertNotIn("originRequest", conf["ingress"][-1])

    def test_a_record_that_sbx_did_not_make_is_not_taken(self):
        self.cf.records["x"] = {"id": "x", "type": "A", "name": "lab-3000.sbx-previews.example", "content": "1.2.3.4"}
        with self.assertRaises(previews.PreviewError):
            self.publish()
        self.assertEqual(self.cf.records["x"]["content"], "1.2.3.4")

    def test_withdraw_removes_the_name_first_and_reports_the_last_one(self):
        self.publish(policy="team")
        self.publish(fqdn="lab-4000.sbx-previews.example")
        self.cf.calls.clear()
        self.assertFalse(self.pv.withdraw("sbx-lab", "lab-3000.sbx-previews.example"))
        writes = [(m, p) for m, p, _ in self.cf.calls if m != "GET"]
        self.assertEqual(writes[0][0], "DELETE")
        self.assertIn("/dns_records/", writes[0][1])
        self.assertEqual(len(self.cf.apps), 1, "the team application went with it")
        self.assertTrue(self.pv.withdraw("sbx-lab", "lab-4000.sbx-previews.example"))

    def test_remove_all_leaves_nothing_of_the_sandbox(self):
        self.publish(policy="team")
        self.publish(fqdn="other-1.sbx-previews.example", host="sbx-other")
        gone = self.pv.remove_all("sbx-lab")
        self.assertEqual(gone, ["lab-3000.sbx-previews.example"])
        self.assertEqual([t["name"] for t in self.cf.tunnels.values()], ["sbx-other"])
        self.assertEqual([r["name"] for r in self.cf.records.values()], ["other-1.sbx-previews.example"])
        self.assertEqual([a["domain"] for a in self.cf.apps.values()], ["*.sbx-previews.example"])


class PolicyFileTest(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "previews.toml"
            path.write_text(text)
            return previews.load_policies(path)

    def test_emails_and_domains(self):
        got = self.load('[policy.me]\nemails = ["me@example.com"]\n'
                        '[policy.acme]\nemail_domains = ["acme.example"]\n')
        self.assertEqual(sorted(got), ["acme", "me"])

    def test_nothing_but_emails_and_domains_can_be_written(self):
        for text in ('[policy.me]\nemails = ["me@example.com"]\n[policy.all]\neveryone = true\n',
                     '[policy.me]\nemails = ["me@example.com"]\ndecision = "bypass"\n',
                     '[policy.me]\nemails = ["*"]\n',
                     '[policy.me]\nemails = []\n',
                     '[policy.team]\nemails = ["a@example.com"]\n'):
            with self.assertRaises(ConfigError, msg=text):
                self.load(text)

    def test_labels(self):
        self.assertEqual(previews.label_for("sbx-lab", 3000, None), "lab-3000")
        self.assertEqual(previews.label_for("sbx-lab", 3000, "demo"), "demo")
        for bad in ("Demo", "a.b", "-x", "x" * 64):
            with self.assertRaises(previews.PreviewError):
                previews.label_for("sbx-lab", 3000, bad)


if __name__ == "__main__":
    unittest.main()


class PublishCommandTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "previews.toml").write_text('[policy.me]\nemails = ["me@example.com"]\n')
        env = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(home)})
        env.start()
        self.addCleanup(env.stop)
        self.cf = FakeCloudflare()

    def run_publish(self, *argv, sidecar=True):
        from sbxlib import cli
        from sbxlib.run import Runner
        box = mock.Mock(hostname="sbx-lab")
        pve = mock.Mock()
        pve.require.return_value = box
        pve.sidecars.return_value = {"sbx-lab": mock.Mock()} if sidecar else {}
        calls = []
        runner = Runner(responder=lambda argv_, data: calls.append((argv_, data)) or "")
        args = cli.build_parser().parse_args(["publish", *argv])
        with mock.patch("sbxlib.cli._pve", return_value=pve):
            code = cli.cmd_publish(args, cfg(), runner, cf=self.cf)
        return code, calls

    def test_the_token_reaches_the_sidecar_on_stdin_only(self):
        code, calls = self.run_publish("lab", "3000")
        self.assertEqual(code, 0)
        ssh = [c for c in calls if c[0][0] == "ssh"]
        self.assertTrue(any("sbx-sidecar-apply --tunnel-token" in " ".join(a) for a, _ in ssh))
        self.assertTrue(any(d and b"token-of-" in d for _, d in ssh))
        self.assertFalse([a for a, _ in calls if "token-of-" in " ".join(a)])
        # The sidecar's own port, not the sandbox's: the ssh goes to 2222.
        self.assertTrue(all("2222" in " ".join(a) for a, _ in ssh))

    def test_a_sidecar_without_cloudflared_is_refused_before_cloudflare(self):
        from sbxlib import cli
        from sbxlib.inputs import InputError
        from sbxlib.run import Result, Runner
        pve = mock.Mock()
        pve.require.return_value = mock.Mock(hostname="sbx-lab")
        pve.sidecars.return_value = {"sbx-lab": mock.Mock()}
        runner = Runner(responder=lambda a, d: Result(1) if "cloudflared" in " ".join(a) else "")
        args = cli.build_parser().parse_args(["publish", "lab", "3000"])
        with mock.patch("sbxlib.cli._pve", return_value=pve), self.assertRaises(InputError):
            cli.cmd_publish(args, cfg(), runner, cf=self.cf)
        self.assertFalse(self.cf.calls)

    def test_a_sandbox_without_a_sidecar_is_refused_before_cloudflare(self):
        from sbxlib.inputs import InputError
        with self.assertRaises(InputError):
            self.run_publish("lab", "3000", sidecar=False)
        self.assertFalse([c for c in self.cf.calls if c[0] != "GET"])
