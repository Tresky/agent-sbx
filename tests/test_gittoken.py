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
        sidecar, on stdin. The VM's credential file names the placeholder."""
        calls, stdins = [], []

        def responder(args, data):
            calls.append(list(args))
            stdins.append(data)
            if args[0] == "git" and args[1] == "-C":
                done = subprocess.run(args, capture_output=True)
                return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
            if args[0] == "security" and args[1] == "find-generic-password":
                return Result(0, TOKEN + "\n", "")
            if args[0] == "ssh" and "/etc/sbx/sidecar/secret" in args[-1]:
                return Result(0, "the-placeholder\n", "")
            if args[0] == "ssh" and "--git-hosts" in args[-1]:
                return Result(0, "github.com\n", "")
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
        # Not this sandbox's own project: the token joins the ones it holds.
        self.assertTrue(any("sbx-sidecar-apply --git-add app github.com x-access-token" in " ".join(c) for c in ssh))
        self.assertIn(f"{TOKEN}\n".encode(), stdins)
        self.assertIn(b"http://sbx:the-placeholder@10.79.0.1:8080\n", stdins)
        self.assertFalse([d for d in stdins if d and b"http" in d and TOKEN.encode() in d],
                         "the VM's credential file never holds the real token")



class NamedTokenTest(unittest.TestCase):
    """A token that names no repository and belongs to no project. A sandbox
    holds any number of them, and a clone shows what they reach."""

    PLACEHOLDER = "the-placeholder"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "cfg"
        self.home.mkdir()
        (self.home / "config.toml").write_text("")
        (self.home / "id_ed25519").write_text("PRIV")
        self.env = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(self.home)})
        self.env.start()
        self.keychain = {}
        self.calls, self.stdins, self.puts = [], [], []
        self.hosts = "github.com\n"

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def responder(self, args, data):
        self.calls.append(list(args))
        self.stdins.append(data)
        if args[0] == "security":
            service = args[args.index("-s") + 1]
            if args[1] == "add-generic-password":
                self.keychain[service] = args[args.index("-w") + 1]
                return ""
            if args[1] == "find-generic-password":
                value = self.keychain.get(service)
                return Result(0, value + "\n", "") if value else Result(44, "", "not found")
        if args[0] == "git" and args[1] == "-C":
            done = subprocess.run(args, capture_output=True)
            return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
        if args[0] == "ssh" and "/etc/sbx/sidecar/secret" in args[-1]:
            return Result(0, self.PLACEHOLDER + "\n", "")
        if args[0] == "ssh" and "--git-hosts" in args[-1]:
            return Result(0, self.hosts, "")
        return ""

    def api(self, tags="sbx;sbx-agent"):
        puts = self.puts

        class Api:
            def __call__(self, method, path, params=None):
                if method == "PUT":
                    puts.append((path, params))
                if path == "/cluster/resources":
                    return [{"type": "qemu", "vmid": 9101, "name": "sbx-lab", "node": "pve", "status": "running",
                             "tags": tags},
                            {"type": "qemu", "vmid": 9102, "name": "sbx-lab-sc", "node": "pve", "status": "running",
                             "tags": "sbx-sidecar;sbx-of-sbx-lab;sbx-tpl-sidecar"}]
                return None
        return Api()

    def run_cmd(self, *argv, api=None):
        with mock.patch("sbxlib.cli.getpass.getpass", return_value=TOKEN), \
             mock.patch("sbxlib.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            return cli.main(["git-token", *argv], runner=Runner(responder=self.responder), api=api)

    def stored(self, name="work", host="github.com"):
        self.keychain[f"sbx-git-{name}"] = TOKEN
        (self.home / "bindings").mkdir(exist_ok=True)
        gittoken.write_binding(self.home / "bindings" / f"{name}.toml", name, host, "x-access-token")

    def test_it_stores_a_token_and_asks_the_host_nothing(self):
        self.assertEqual(self.run_cmd("--name", "work"), 0)
        self.assertEqual(self.keychain["sbx-git-work"], TOKEN)
        self.assertEqual(load_bindings(self.home / "bindings" / "work.toml").git.host, "github.com")
        self.assertFalse((self.home / "projects.toml").exists(), "a token registers no repository")
        self.assertFalse([c for c in self.calls if c[0] in ("curl", "git", "ssh")])

    def test_the_host_is_an_option(self):
        self.assertEqual(self.run_cmd("--name", "work", "--host", "gitlab.com"), 0)
        self.assertEqual(load_bindings(self.home / "bindings" / "work.toml").git.host, "gitlab.com")

    def test_a_name_that_is_not_safe_stores_nothing(self):
        for bad in ("../evil", "a/b", "Work", ".hidden", "-x", "a b"):
            with self.subTest(name=bad):
                self.assertEqual(self.run_cmd(f"--name={bad}"), 1)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.home / "bindings").exists())

    def test_it_takes_a_project_or_a_name(self):
        self.assertEqual(self.run_cmd(), 1)
        self.assertEqual(self.run_cmd("some/project", "--name", "work"), 1)
        self.assertEqual(self.run_cmd("some/project", "--drop", "lab"), 1)
        self.assertEqual(self.calls, [])

    def ssh(self):
        return [" ".join(c) for c in self.calls if c[0] == "ssh"]

    def test_push_wires_a_sandbox_that_has_no_project(self):
        self.stored()
        with mock.patch("sbxlib.cli.getpass.getpass", side_effect=AssertionError("no prompt with a stored token")):
            self.assertEqual(self.run_cmd("--name", "work", "--push", "lab", api=self.api()), 0)
        self.assertFalse([c for c in self.calls if c[0] == "ssh" and TOKEN in " ".join(c)])
        # The sidecar gets the real token on stdin, and the VM gets the placeholder.
        self.assertIn(f"{TOKEN}\n".encode(), self.stdins)
        self.assertIn(f"http://sbx:{self.PLACEHOLDER}@10.79.0.1:8080\n".encode(), self.stdins)
        add = next(c for c in self.ssh() if "--git-add" in c)
        self.assertIn("-p 2222", add)
        self.assertIn("--git-add work github.com x-access-token", add)
        rewrite = next(c for c in self.ssh() if "insteadOf" in c)
        self.assertIn("dev@sbx-lab", rewrite)
        self.assertNotIn("-p 2222", rewrite)
        for remote in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
            self.assertIn(f"url.http://10.79.0.1:8080/git/github.com/.insteadOf {remote}", rewrite)
        self.assertIn(("/nodes/pve/qemu/9101/config", {"tags": "sbx;sbx-agent;sbx-git-work"}), self.puts)

    def test_push_brings_the_scripts_of_this_checkout_first(self):
        """A sidecar from an older template has no --git-add."""
        self.stored()
        self.run_cmd("--name", "work", "--push", "lab", api=self.api())
        ssh = self.ssh()
        script = next(i for i, c in enumerate(ssh) if "/usr/local/lib/sbx/sidecar.py" in c)
        self.assertLess(script, next(i for i, c in enumerate(ssh) if "--git-add" in c))

    def test_a_second_token_joins_the_first(self):
        self.stored("other")
        self.hosts = "github.com\ngitlab.com\n"
        self.assertEqual(self.run_cmd("--name", "other", "--push", "lab", api=self.api("sbx;sbx-agent;sbx-git-work")), 0)
        self.assertIn(("/nodes/pve/qemu/9101/config", {"tags": "sbx;sbx-agent;sbx-git-work;sbx-git-other"}), self.puts)
        rewrite = next(c for c in self.ssh() if "insteadOf" in c)
        self.assertIn("/git/github.com/.insteadOf", rewrite)
        self.assertIn("/git/gitlab.com/.insteadOf", rewrite)

    def test_drop_takes_the_token_out_and_rewires(self):
        self.stored()
        self.hosts = ""
        self.assertEqual(self.run_cmd("--name", "work", "--drop", "lab", api=self.api("sbx;sbx-agent;sbx-git-work")), 0)
        self.assertTrue([c for c in self.ssh() if "--git-drop work" in c])
        self.assertIn(("/nodes/pve/qemu/9101/config", {"tags": "sbx;sbx-agent"}), self.puts)
        self.assertEqual(self.keychain["sbx-git-work"], TOKEN, "the Mac keeps the token")
        self.assertFalse([c for c in self.ssh() if "--add" in c], "no host is left to rewrite")

    def test_remove_forgets_it_on_this_machine(self):
        self.stored()
        self.assertEqual(self.run_cmd("--name", "work", "--remove"), 0)
        self.assertFalse((self.home / "bindings" / "work.toml").exists())
        self.assertTrue([c for c in self.calls if c[:2] == ["security", "delete-generic-password"]])

    def test_list_shows_each_token_and_who_holds_it(self):
        self.stored("work", "gitlab.com")
        self.stored("unused")
        (self.home / "bindings" / "inputs-only.toml").write_text('[inputs.key]\nvalue = "x"\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_cmd("--list", api=self.api("sbx;sbx-agent;sbx-git-work")), 0)
        lines = {line.split()[0]: line.split() for line in out.getvalue().splitlines()}
        self.assertEqual(lines["work"], ["work", "gitlab.com", "yes", "sbx-lab"])
        self.assertEqual(lines["unused"], ["unused", "github.com", "yes", "-"])
        self.assertNotIn("inputs-only", lines)
        self.assertNotIn(TOKEN, out.getvalue())


class OwnProjectPushTest(ProjectCase):
    def test_a_project_token_replaces_itself_in_its_own_sandbox(self):
        calls, stdins, puts = [], [], []

        def responder(args, data):
            calls.append(" ".join(args))
            stdins.append(data)
            if args[0] == "git" and args[1] == "-C":
                done = subprocess.run(args, capture_output=True)
                return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
            if args[0] == "security" and args[1] == "find-generic-password":
                return Result(0, TOKEN + "\n", "")
            if args[0] == "ssh" and "/etc/sbx/sidecar/secret" in args[-1]:
                return Result(0, "the-placeholder\n", "")
            if args[0] == "ssh" and "--git-hosts" in args[-1]:
                return Result(0, "github.com\n", "")
            return ""

        class Api:
            def __call__(self, method, path, params=None):
                if method == "PUT":
                    puts.append((path, params))
                if path == "/cluster/resources":
                    return [{"type": "qemu", "vmid": 9101, "name": "sbx-lab", "node": "pve", "status": "running",
                             "tags": "sbx;sbx-agent;sbx-proj-app"},
                            {"type": "qemu", "vmid": 9102, "name": "sbx-lab-sc", "node": "pve", "status": "running",
                             "tags": "sbx-sidecar;sbx-of-sbx-lab;sbx-tpl-sidecar"}]
                return None

        (self.home / "id_ed25519").write_text("PRIV")
        code = cli.main(["git-token", str(self.app), "--push", "lab"], runner=Runner(responder=responder), api=Api())
        self.assertEqual(code, 0)
        self.assertTrue([c for c in calls if "sbx-sidecar-apply --git-token" in c])
        self.assertFalse([c for c in calls if "--git-add" in c])
        self.assertEqual(puts, [], "no tag: the project's own tag already says it")
        # The host goes in first: the token never reaches another host.
        upstream = next(i for i, c in enumerate(calls) if "SBX_GIT_UPSTREAM=https://github.com" in c)
        self.assertLess(upstream, next(i for i, c in enumerate(calls) if "--git-token" in c))


class RepoTest(unittest.TestCase):
    """A repository: registered with no token, cloned with whatever the sandbox has."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "cfg"
        self.home.mkdir()
        (self.home / "config.toml").write_text("")
        (self.home / "id_ed25519").write_text("PRIV")
        self.env = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(self.home)})
        self.env.start()
        self.calls = []
        self.clone_fails = set()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def responder(self, args, data):
        self.calls.append(" ".join(args))
        if args[0] == "ssh" and "git clone" in args[-1]:
            for name in self.clone_fails:
                if f"code/{name}" in args[-1]:
                    return Result(128, "", "Cloning...\nfatal: unable to access: The requested URL returned error: 403")
        return ""

    def api(self, tags="sbx;sbx-agent"):
        class Api:
            def __call__(self, method, path, params=None):
                if path == "/cluster/resources":
                    return [{"type": "qemu", "vmid": 9101, "name": "sbx-lab", "node": "pve", "status": "running",
                             "tags": tags}]
                return None
        return Api()

    def repo(self, *argv, api=None):
        return cli.main(["repo", *argv], runner=Runner(responder=self.responder), api=api)

    def registered(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.repo("list"), 0)
        return out.getvalue()

    def test_it_registers_with_no_token_and_no_network(self):
        self.assertEqual(self.repo("add", "git@github.com:me/api.git"), 0)
        self.assertEqual(self.repo("add", "me/web"), 0)
        self.assertEqual(self.calls, [], "registering a repository asks nothing of git or of a host")
        text = self.registered()
        self.assertIn("api", text)
        self.assertIn("git@github.com:me/api.git", text)
        self.assertIn("https://github.com/me/web", text)
        self.assertFalse((self.home / "bindings").exists(), "a repository makes no token binding")

    def test_a_name_in_use_is_refused_and_another_name_works(self):
        self.assertEqual(self.repo("add", "me/api"), 0)
        self.assertEqual(self.repo("add", "you/api"), 1)
        self.assertEqual(self.repo("add", "me/api"), 0, "the same URL again changes nothing")
        self.assertEqual(self.repo("add", "you/api", "--name", "your-api"), 0)
        self.assertIn("https://github.com/you/api", self.registered())

    def test_a_bad_name_or_url_is_refused(self):
        self.assertEqual(self.repo("add", "me/api", "--name", "../x"), 1)
        self.assertEqual(self.repo("add", "--", "-upload-pack=x"), 1)
        self.assertEqual(self.repo("add", "file:///etc"), 1)
        self.assertNotIn("api", self.registered())

    def test_rm(self):
        self.repo("add", "me/api")
        self.assertEqual(self.repo("rm", "api"), 0)
        self.assertEqual(self.repo("rm", "api"), 1)

    def test_clone_takes_a_name_a_url_or_owner_repo(self):
        self.repo("add", "me/api")
        code = self.repo("clone", "lab", "api", "git@github.com:me/web.git", "me/docs", api=self.api())
        self.assertEqual(code, 0)
        clones = [c for c in self.calls if "git clone" in c]
        self.assertEqual(len(clones), 3)
        self.assertIn("https://github.com/me/api code/api", clones[0])
        self.assertIn("git@github.com:me/web.git code/web", clones[1])
        self.assertIn("https://github.com/me/docs code/docs", clones[2])
        self.assertTrue(all("GIT_TERMINAL_PROMPT=0" in c for c in clones), "a clone never waits at a prompt")
        self.assertFalse([c for c in clones if "ForwardAgent=yes" in c], "an agent sandbox never gets the SSH agent")

    def test_one_failed_clone_does_not_stop_the_next(self):
        self.clone_fails = {"web"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = self.repo("clone", "lab", "me/web", "me/api", api=self.api())
        self.assertEqual(code, 1)
        self.assertEqual(len([c for c in self.calls if "git clone" in c]), 2)
        self.assertIn("web: not cloned: fatal: unable to access", err.getvalue())

    def test_a_personal_sandbox_clones_with_the_forwarded_agent(self):
        self.repo("clone", "lab", "me/api", api=self.api("sbx;sbx-personal"))
        self.assertTrue([c for c in self.calls if "git clone" in c and "ForwardAgent=yes" in c])


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
