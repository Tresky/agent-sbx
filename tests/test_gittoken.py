"""`sbx git-token`: the token goes to the keychain and the binding, never into
an argument of the check, and a token that does not cover a repository stores
nothing (a controlled pair with one that does)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cli, gittoken
from sbxlib.inputs import load_bindings
from sbxlib.run import Result, Runner

TOKEN = "github_pat_11AAAA_secret"


class RepoPathTest(unittest.TestCase):
    def test_both_forms(self):
        self.assertEqual(gittoken.repo_path("git@github.com:example/ui-kit.git"), ("github.com", "example/ui-kit"))
        self.assertEqual(gittoken.repo_path("https://github.com/example/web-app.git"), ("github.com", "example/web-app"))
        self.assertEqual(gittoken.repo_path("ssh://git@gitlab.com:2222/a/b/c.git"), ("gitlab.com", "a/b/c"))
        self.assertIsNone(gittoken.repo_path("not a url"))


class BindingFileTest(unittest.TestCase):
    def test_git_block_is_set_and_the_rest_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.toml"
            path.write_text("# my notes\n[inputs.key]\ncommand = [\"op\", \"read\", \"x\"]\n\n[git]\ntoken_command = [\"old\"]\n")
            gittoken.write_binding(path, "app", "github.com", "x-access-token")
            text = path.read_text()
            self.assertIn("# my notes", text)
            self.assertEqual(text.count("[git]"), 1)
            b = load_bindings(path)
            self.assertEqual(b.git.token_command[-2], "sbx-git-app")
            self.assertEqual(b.inputs["key"]["command"][0], "op")
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            # A non-default host and user name are written; the defaults are not.
            gittoken.write_binding(path, "app", "gitlab.com", "oauth2")
            self.assertEqual((load_bindings(path).git.host, load_bindings(path).git.username), ("gitlab.com", "oauth2"))
            self.assertTrue(gittoken.remove_binding(path))
            self.assertIsNone(load_bindings(path).git)
            self.assertIn("# my notes", path.read_text())


class CommandTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.home = tmp / "cfg"
        self.home.mkdir()
        (self.home / "config.toml").write_text("")
        self.app = tmp / "app"
        (self.app / ".sandbox").mkdir(parents=True)
        (self.app / ".sandbox/sandbox.toml").write_text(
            '[[input]]\nname = "ui"\nkind = "repo"\nurl = "git@github.com:me/ui.git"\ndest = "../ui"\n')
        subprocess.run(["git", "init", "-q", str(self.app)], check=True)
        subprocess.run(["git", "-C", str(self.app), "remote", "add", "origin", "git@github.com:me/app.git"], check=True)
        self.env = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def run_cmd(self, codes, *extra):
        """codes: http code per repository path, in order of the curl calls."""
        calls, stdins = [], []

        def responder(args, data):
            calls.append(list(args))
            stdins.append(data)
            if args[0] == "git" and args[1] == "-C":
                done = subprocess.run(args, capture_output=True)
                return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
            if args[0] == "curl":
                body = data.decode()
                for path, code in codes.items():
                    if f"/repos/{path}" in body:
                        return code
                return "000"
            return ""
        with mock.patch("sbxlib.cli.getpass.getpass", return_value=TOKEN), \
             mock.patch("sbxlib.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            code = cli.main(["git-token", str(self.app), *extra], runner=Runner(responder=responder))
        return code, calls, stdins

    def test_stores_and_binds_when_the_token_covers_every_repository(self):
        code, calls, stdins = self.run_cmd({"me/app": "200", "me/ui": "200"})
        self.assertEqual(code, 0)
        # The token reached curl on stdin only, and the keychain through `security`.
        for c in calls:
            if c[0] == "curl":
                self.assertNotIn(TOKEN, " ".join(c))
        self.assertTrue(any(TOKEN in d.decode() for d in stdins if d))
        keychain = next(c for c in calls if c[0] == "security" and c[1] == "add-generic-password")
        self.assertEqual(keychain[keychain.index("-s") + 1], "sbx-git-app")
        self.assertEqual(keychain[-1], TOKEN)
        b = load_bindings(self.home / "bindings" / "app.toml")
        self.assertEqual(b.git.token_command[-2], "sbx-git-app")

    def test_a_token_that_misses_one_repository_stores_nothing(self):
        # Controlled pair with the test above: same command, one 404.
        code, calls, _ = self.run_cmd({"me/app": "200", "me/ui": "404"})
        self.assertEqual(code, 1)
        self.assertFalse([c for c in calls if c[0] == "security"])
        self.assertFalse((self.home / "bindings" / "app.toml").exists())

    def test_no_check_skips_the_host(self):
        code, calls, _ = self.run_cmd({}, "--no-check")
        self.assertEqual(code, 0)
        self.assertFalse([c for c in calls if c[0] == "curl"])

    def test_remove(self):
        self.run_cmd({"me/app": "200", "me/ui": "200"})
        code, calls, _ = self.run_cmd({}, "--remove")
        self.assertEqual(code, 0)
        self.assertTrue(any(c[0] == "security" and c[1] == "delete-generic-password" for c in calls))
        self.assertFalse((self.home / "bindings" / "app.toml").exists())



class UrlProjectTest(unittest.TestCase):
    """A project named by URL alone: no checkout on this Mac."""
    URL = "https://github.com/example-org/web-app"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "config.toml").write_text("")
        env = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def test_git_token_stores_a_token_without_cloning(self):
        calls = []
        runner = Runner(responder=lambda a, d: calls.append(list(a)) or ("200" if a[0] == "curl" else ""))
        with mock.patch("sys.stdin") as stdin:
            stdin.readline.return_value = TOKEN + "\n"
            code = cli.main(["git-token", self.URL, "--stdin"], runner=runner)
        self.assertEqual(code, 0)
        self.assertFalse([c for c in calls if c[0] == "git"], "nothing may be cloned for a token")
        self.assertEqual(load_bindings(self.home / "bindings" / "web-app.toml").git.token_command[-2], "sbx-git-web-app")

    def test_the_manifest_is_read_with_the_projects_token_and_the_file_is_gone(self):
        (self.home / "bindings").mkdir()
        (self.home / "bindings" / "web-app.toml").write_text('[git]\ntoken_command = ["print-token"]\n')
        seen = {}

        def responder(argv, data):
            if argv[0] == "print-token":
                return TOKEN + "\n"
            if argv[0] == "git" and "clone" in argv:
                helper = next(a for a in argv if a.startswith("credential.helper=!"))
                path = Path(helper.split("cat ", 1)[1].split(";", 1)[0].strip("'"))
                seen.update(argv=argv, path=path, mode=path.stat().st_mode & 0o777, text=path.read_text())
            return ""
        cli.resolve_project(self.URL, None, None, Runner(responder=responder))
        self.assertNotIn(TOKEN, " ".join(seen["argv"]))
        self.assertIn("credential.helper=", seen["argv"], "a helper of the user's is switched off")
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["text"], f"username=x-access-token\npassword={TOKEN}\n")
        self.assertFalse(seen["path"].exists())

    def test_without_a_stored_token_the_clone_is_plain(self):
        calls = []
        cli.resolve_project(self.URL, None, None, Runner(responder=lambda a, d: calls.append(list(a)) or ""))
        clone = next(c for c in calls if "clone" in c)
        self.assertFalse([a for a in clone if a.startswith("credential.helper")])


if __name__ == "__main__":
    unittest.main()
