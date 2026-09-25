"""The project registry fills itself from any command that sees a checkout,
and --project <name> then resolves by name."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cli, projects
from sbxlib.run import Runner


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "cfg"
        self.home.mkdir()
        (self.home / "config.toml").write_text("")
        self.app = self.tmp / "app"
        self.app.mkdir()
        subprocess.run(["git", "init", "-q", str(self.app)], check=True)
        subprocess.run(["git", "-C", str(self.app), "remote", "add", "origin", "git@github.com:me/app.git"], check=True)
        self.env = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def runner(self, seen=None):
        def responder(args, data):
            if seen is not None:
                seen.append(list(args))
            if args[0] == "git":
                done = subprocess.run(args, capture_output=True)
                from sbxlib.run import Result
                return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
            if args[0] == "security":
                from sbxlib.run import Result
                return Result(1)  # no token in the keychain
            return ""
        return Runner(responder=responder)

    def test_a_checkout_is_recorded_and_then_resolves_by_name(self):
        self.assertEqual(projects.load(), {})
        cli.main(["inputs", str(self.app)], runner=self.runner())
        entry = projects.load()["app"]
        self.assertEqual((entry.checkout, entry.url), (str(self.app.resolve()), "git@github.com:me/app.git"))
        # By name now: the same project, from the recorded checkout.
        project = cli.resolve_project("app", None, None, self.runner(), need_remote=False)
        self.assertEqual((project.name, project.url), ("app", "git@github.com:me/app.git"))

    def test_an_unknown_name_is_a_clear_error(self):
        with self.assertRaises(projects.ProjectError) as ctx:
            cli.resolve_project("nope", None, None, self.runner(), need_remote=False)
        self.assertIn("sbx projects", str(ctx.exception))

    def test_projects_lists_and_project_rm_forgets(self):
        cli.main(["inputs", str(self.app)], runner=self.runner())
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["projects"], runner=self.runner())
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("PROJECT", text)
        self.assertRegex(text, r"app\s+no\s+\S*app")
        self.assertEqual(cli.main(["project", "rm", "app"], runner=self.runner()), 0)
        self.assertEqual(projects.load(), {})
        self.assertEqual(cli.main(["project", "rm", "app"], runner=self.runner()), 1)

    def test_tag_folds_the_name_to_what_proxmox_accepts(self):
        self.assertEqual(projects.tag("My.Web_App"), "sbx-proj-my.web_app")
        self.assertEqual(projects.tag("my app!"), "sbx-proj-my-app-")

    def test_registry_file_round_trip(self):
        path = self.tmp / "p.toml"
        projects.record("a", self.app, "git@github.com:me/a.git", path)
        projects.record("b", None, "https://github.com/me/b.git", path)
        projects.record("a", None, "", path)  # refresh with nothing new keeps the old values
        loaded = projects.load(path)
        self.assertEqual(loaded["a"].url, "git@github.com:me/a.git")
        self.assertEqual(loaded["b"].checkout, "")
        self.assertTrue(projects.forget("b", path))
        self.assertEqual(list(projects.load(path)), ["a"])


if __name__ == "__main__":
    unittest.main()
