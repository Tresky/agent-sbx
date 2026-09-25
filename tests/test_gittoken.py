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

    def test_push_installs_the_stored_token_into_a_running_sandbox(self):
        """With a token in the keychain, --push asks for none: it writes the
        credential file into the VM over ssh, on stdin, and sets the rewrite."""
        calls, stdins = [], []

        def responder(args, data):
            calls.append(list(args))
            stdins.append(data)
            if args[0] == "git" and args[1] == "-C":
                done = subprocess.run(args, capture_output=True)
                return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
            if args[0] == "security" and args[1] == "find-generic-password":
                return Result(0, TOKEN + "\n", "")
            return ""

        class Api:
            def __call__(self, method, path, params=None):
                if path == "/cluster/resources":
                    return [{"type": "qemu", "vmid": 9101, "name": "sbx-lab", "node": "pve", "status": "running",
                             "tags": "sbx;sbx-personal"}]
                return None

        (self.home / "id_ed25519").write_text("PRIV")
        with mock.patch("sbxlib.cli.getpass.getpass", side_effect=AssertionError("no prompt with a stored token")):
            code = cli.main(["git-token", str(self.app), "--push", "lab"], runner=Runner(responder=responder), api=Api())
        self.assertEqual(code, 0)
        # The credential travels on stdin to the VM, never in an argv.
        self.assertFalse([c for c in calls if TOKEN in " ".join(c)])
        self.assertIn(f"https://x-access-token:{TOKEN}@github.com\n".encode(), stdins)
        ssh = [c for c in calls if c[0] == "ssh"]
        self.assertTrue(any(".git-credentials" in " ".join(c) for c in ssh))
        rewrite = next(c for c in ssh if "insteadOf" in " ".join(c))
        self.assertIn("--replace-all", " ".join(rewrite))      # a second push must not add a third value


if __name__ == "__main__":
    unittest.main()
