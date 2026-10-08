"""`sbx git-token`: the token goes to the keychain and the binding, never into
an argument of the check, and a token that does not cover a repository stores
nothing (a controlled pair with one that does)."""
import contextlib
import io
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


class ProjectCase(unittest.TestCase):
    """A checkout with a GitHub origin, and a config directory of its own."""

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


class CommandTest(ProjectCase):
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

    def test_push_to_a_sandbox_with_a_sidecar_gives_the_token_to_the_sidecar_only(self):
        """The sandbox holds the placeholder; the real token goes to its
        sidecar, on stdin, and the VM gets no credential file."""
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
                             "tags": "sbx;sbx-agent"},
                            {"type": "qemu", "vmid": 9102, "name": "sbx-lab-sc", "node": "pve", "status": "running",
                             "tags": "sbx-sidecar;sbx-of-sbx-lab;sbx-tpl-sidecar"}]
                return None

        (self.home / "id_ed25519").write_text("PRIV")
        code = cli.main(["git-token", str(self.app), "--push", "lab"], runner=Runner(responder=responder), api=Api())
        self.assertEqual(code, 0)
        self.assertFalse([c for c in calls if TOKEN in " ".join(c)])
        ssh = [c for c in calls if c[0] == "ssh"]
        self.assertTrue(any("sbx-sidecar-apply --git-token" in " ".join(c) for c in ssh))
        self.assertFalse(any(".git-credentials" in " ".join(c) for c in ssh))
        self.assertIn(f"{TOKEN}\n".encode(), stdins)



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


class GitLabCheckTest(unittest.TestCase):
    """GitLab is asked through git's smart-HTTP handshake: a token made for
    git alone cannot call its REST API."""

    def check(self, upload: str, receive: str = "200"):
        configs = []

        def responder(args, data):
            body = data.decode()
            configs.append(body)
            return upload if "git-upload-pack" in body else receive
        [c] = gittoken.check_token(Runner(responder=responder), "glpat-x", "gitlab.com",
                                   ["git@gitlab.com:grp/sub/app.git"], "oauth2")
        return c.status, configs

    def test_statuses(self):
        status, configs = self.check("200", "200")
        self.assertEqual(status, "ok")
        self.assertIn('user = "oauth2:glpat-x"', configs[0])
        self.assertIn("https://gitlab.com/grp/sub/app.git/info/refs?service=git-upload-pack", configs[0])
        self.assertIn("service=git-receive-pack", configs[1])
        self.assertEqual(self.check("200", "403")[0], "read only")
        self.assertEqual(self.check("401")[0], "bad token")
        self.assertEqual(self.check("404")[0], "no access")
        # A failed read asks nothing about push.
        self.assertEqual(len(self.check("404")[1]), 1)

    def test_only_gitlab_hosts_are_asked_this_way(self):
        self.assertTrue(gittoken.is_gitlab("gitlab.com"))
        self.assertTrue(gittoken.is_gitlab("gitlab.example.com"))
        self.assertFalse(gittoken.is_gitlab("git.example.com"))
        [c] = gittoken.check_token(Runner(responder=lambda a, d: "200"), "t", "git.example.com",
                                   ["https://git.example.com/a/b.git"])
        self.assertEqual(c.status, "not checked")


class GitLabCommandTest(ProjectCase):
    """The same command for a project whose origin is on GitLab: the host
    comes from the remote, with no --host."""

    def setUp(self):
        super().setUp()
        subprocess.run(["git", "-C", str(self.app), "remote", "set-url", "origin", "git@gitlab.com:me/app.git"],
                       check=True)
        (self.app / ".sandbox/sandbox.toml").write_text("")

    def run_gitlab(self, upload="200", receive="200", *extra):
        calls, stdins = [], []

        def responder(args, data):
            calls.append(list(args))
            stdins.append(data)
            if args[0] == "git" and args[1] == "-C":
                done = subprocess.run(args, capture_output=True)
                return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
            if args[0] == "curl":
                return upload if "git-upload-pack" in data.decode() else receive
            return ""
        with mock.patch("sbxlib.cli.getpass.getpass", return_value=TOKEN), \
             mock.patch("sbxlib.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            code = cli.main(["git-token", str(self.app), *extra], runner=Runner(responder=responder))
        return code, calls, stdins

    def test_the_host_comes_from_the_remote(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, calls, stdins = self.run_gitlab()
        self.assertEqual(code, 0)
        self.assertIn("https://gitlab.com/-/user_settings/personal_access_tokens", out.getvalue())
        self.assertTrue(any(b"https://gitlab.com/me/app.git/info/refs" in d for d in stdins if d))
        self.assertFalse(any(b"api.github.com" in d for d in stdins if d))
        for c in calls:
            if c[0] == "curl":
                self.assertNotIn(TOKEN, " ".join(c))
        b = load_bindings(self.home / "bindings" / "app.toml")
        self.assertEqual((b.git.host, b.git.username), ("gitlab.com", "x-access-token"))

    def test_a_token_gitlab_does_not_know_stores_nothing(self):
        code, calls, _ = self.run_gitlab("401")
        self.assertEqual(code, 1)
        self.assertFalse([c for c in calls if c[0] == "security"])

    def test_a_read_only_token_is_stored_with_a_warning(self):
        with mock.patch("sbxlib.cli.warn") as warned:
            code, _, _ = self.run_gitlab("200", "403")
        self.assertEqual(code, 0)
        self.assertIn("not push", warned.call_args[0][0])

    def test_push_uses_the_host_the_stored_token_is_bound_to(self):
        """With no --host, --push uses the host the stored token is bound to."""
        self.run_gitlab()
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
        code = cli.main(["git-token", str(self.app), "--push", "lab"], runner=Runner(responder=responder), api=Api())
        self.assertEqual(code, 0)
        self.assertIn(f"https://x-access-token:{TOKEN}@gitlab.com\n".encode(), stdins)
