"""sbx cloudflare-setup: a fresh account made ready, idempotently; what it
cannot do is reported, never guessed; the token is never an argument."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cfsetup, cli, previews
from sbxlib.run import Runner
from tests.test_previews import FakeCloudflare

ACCT_ID = "0123456789abcdef0123456789abcdef"
ZONE = "sbx-previews.example"


class FakeAccount(FakeCloudflare):
    """FakeCloudflare, plus what setup reads: the token, the zone's status,
    the Zero Trust organization and the login methods."""

    def __init__(self, zone_status="active", org=True, idps=(), may_add_idp=True, token_ok=True):
        super().__init__()
        self.zones = [{"id": "z1", "name": ZONE, "status": zone_status, "name_servers": ["a.ns", "b.ns"]}]
        self.org, self.idps, self.may_add_idp, self.token_ok = org, list(idps), may_add_idp, token_ok

    def __call__(self, method, path, body=None):
        base = path.split("?")[0]
        acct = f"/accounts/{ACCT_ID}"
        if base.endswith("/tokens/verify"):
            self.calls.append((method, base, body))
            if not self.token_ok:
                raise previews.PreviewError(f"Cloudflare: GET {base}: Invalid API Token")
            return {"status": "active"}
        if base == f"{acct}/access/organizations":
            self.calls.append((method, base, body))
            if not self.org:
                raise previews.PreviewError(f"Cloudflare: GET {base}: access.api.error.not_enabled")
            return {"auth_domain": "team.cloudflareaccess.com"}
        if base == f"{acct}/access/identity_providers":
            self.calls.append((method, base, body))
            if method == "POST":
                if not self.may_add_idp:
                    raise previews.PreviewError(f"Cloudflare: POST {base}: Authentication error")
                self.idps.append(body)
                return body
            return list(self.idps)
        return super().__call__(method, path.replace(ACCT_ID, "acc1"), body)


class RunTest(unittest.TestCase):
    def run_setup(self, fake, **kw):
        return cfsetup.run(fake, ACCT_ID, ZONE, ["me@example.com"], "336h", **kw)

    def test_a_fresh_account_gets_the_login_the_policy_and_the_wildcard(self):
        fake = FakeAccount()
        rep = self.run_setup(fake)
        self.assertFalse(rep.todo)
        self.assertEqual([i["type"] for i in fake.idps], ["onetimepin"])
        (pol,) = fake.policies.values()
        self.assertEqual((pol["name"], pol["include"]), ("sbx: me", [{"email": {"email": "me@example.com"}}]))
        (app,) = fake.apps.values()
        self.assertEqual((app["domain"], app["session_duration"]), (f"*.{ZONE}", "336h"))
        # Nothing of a tunnel or a DNS record: setup makes Access only.
        self.assertFalse(fake.tunnels or fake.records)

    def test_a_second_run_changes_nothing_but_the_emails(self):
        fake = FakeAccount()
        self.run_setup(fake)
        cfsetup.run(fake, ACCT_ID, ZONE, ["me@example.com", "you@example.com"], "336h")
        self.assertEqual(len(fake.apps), 1)
        self.assertEqual(len(fake.policies), 1)
        self.assertEqual(len(fake.idps), 1)
        (pol,) = fake.policies.values()
        self.assertEqual(len(pol["include"]), 2)

    def test_a_dry_run_writes_nothing(self):
        fake = FakeAccount()
        rep = self.run_setup(fake, dry_run=True)
        self.assertFalse([c for c in fake.calls if c[0] != "GET"])
        self.assertTrue(any(s == "would" for s, _ in rep.lines))

    def test_what_only_the_dashboard_can_do_is_an_error_with_the_step(self):
        for fake, words in ((FakeAccount(zone_status="pending"), "nameservers"),
                            (FakeAccount(org=False), "Zero Trust"),
                            (FakeAccount(token_ok=False), "not valid")):
            with self.assertRaises(cfsetup.SetupError) as ctx:
                self.run_setup(fake)
            self.assertIn(words, str(ctx.exception))
            self.assertFalse(fake.apps, "nothing is made before the checks pass")

    def test_a_token_that_may_not_add_the_login_leaves_a_todo(self):
        rep = self.run_setup(FakeAccount(may_add_idp=False))
        self.assertTrue(rep.todo)

    def test_the_arguments_are_checked_first(self):
        for acct, zone, emails in (("nothex", ZONE, ["a@b.co"]), (ACCT_ID, "No Zone", ["a@b.co"]),
                                   (ACCT_ID, ZONE, []), (ACCT_ID, ZONE, ["*"])):
            with self.assertRaises(cfsetup.SetupError):
                cfsetup.run(FakeAccount(), acct, zone, emails, "336h")


class PreviewsTomlTest(unittest.TestCase):
    def test_me_is_set_and_the_rest_stays(self):
        self.assertIn('[policy.me]\nemails = ["a@example.com"]', cfsetup.previews_toml("", ["a@example.com"]))
        before = '# mine\n[policy.me]\nemails = ["old@example.com"]\n\n[policy.team]\nemails = ["t@example.com"]\n'
        after = cfsetup.previews_toml(before, ["new@example.com"])
        self.assertIn("# mine", after)
        self.assertIn('[policy.team]\nemails = ["t@example.com"]', after)
        self.assertNotIn("old@example.com", after)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "previews.toml"
            path.write_text(after)
            self.assertEqual(previews.load_policies(path)["me"].emails, ["new@example.com"])


class CommandTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(self.home), "SBX_SECRET_STORE": "file",
                                           "CLOUDFLARE_API_TOKEN": "cf-token-secret"})
        env.start()
        self.addCleanup(env.stop)

    def run_cmd(self, *extra):
        calls = []
        runner = Runner(responder=lambda a, d: calls.append(a) or "")
        args = cli.build_parser().parse_args(["cloudflare-setup", "--account-id", ACCT_ID, "--zone", ZONE,
                                              "--email", "me@example.com", *extra])
        code = cli.cmd_cloudflare_setup(args, cli.load_config(), runner, cf=FakeAccount())
        return code, calls

    def test_the_token_comes_from_the_environment_and_is_stored(self):
        code, calls = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertFalse([a for a in calls if "cf-token-secret" in " ".join(a)], "never in an argv")
        self.assertEqual((self.home / "secrets" / "sbx-cloudflare-token").read_text().strip(), "cf-token-secret")
        cfg = cli.load_config()
        self.assertEqual((cfg.preview_zone, cfg.cloudflare_account_id), (ZONE, ACCT_ID))
        self.assertTrue(cfg.cloudflare_token_command)
        self.assertEqual(previews.load_policies()["me"].emails, ["me@example.com"])

    def test_access_only_stores_no_token(self):
        code, _ = self.run_cmd("--access-only")
        self.assertEqual(code, 0)
        self.assertFalse((self.home / "secrets").exists())
        self.assertFalse(cli.load_config().cloudflare_token_command)

    def test_a_dry_run_writes_nothing_here(self):
        code, _ = self.run_cmd("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse((self.home / "previews.toml").exists())
        self.assertFalse((self.home / "config.toml").exists())


if __name__ == "__main__":
    unittest.main()
