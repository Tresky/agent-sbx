"""Remote Control: the sign-in flow is driven through tmux in the sandbox, the
server is a user service, and the profile decides whether it runs at all."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cli, remotecontrol
from sbxlib.config import Config
from sbxlib.run import Runner
from sbxlib.vm import Vm

URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y"


class FlowTest(unittest.TestCase):
    """remotecontrol.* against a fake sandbox: what is typed into tmux, and
    when the login counts as done."""

    def make_vm(self, captures, logs=None):
        """`captures` answer tmux capture-pane in order; `logs` answer the
        server's log tail in order."""
        seen = []
        logs = list(logs or [])

        def responder(args, data):
            seen.append(args[-1])  # the remote command
            cmd = args[-1]
            if "capture-pane" in cmd:
                return captures.pop(0) if captures else ""
            if cmd.startswith("tail -n"):
                return logs.pop(0) if logs else ""
            if "claude auth status" in cmd:
                return '{"loggedIn": true, "authMethod": "claude.ai"}'
            return ""
        cfg = Config(ssh_key="/dev/null")
        return Vm(cfg, Runner(responder=responder), "sbx-lab", "10.77.0.5"), seen

    def test_login_returns_the_url_then_types_the_code(self):
        with mock.patch("sbxlib.remotecontrol.time.sleep"):
            vm, seen = self.make_vm(["Opening browser…", f"visit: {URL}\nPaste code here if prompted >"])
            self.assertEqual(remotecontrol.start_login(vm, "/home/dev/code/app"), URL)
            self.assertTrue(any("tmux new-session -d -s sbx-login" in c and "claude auth login" in c for c in seen))
            vm2, seen2 = self.make_vm(["waiting", "Login successful\nSBX_LOGIN_EXIT=0"])
            remotecontrol.finish_login(vm2, "abc123#def")
            self.assertTrue(any("tmux send-keys -t sbx-login 'abc123#def' Enter" in c for c in seen2))

    def test_a_failed_login_is_an_error_with_the_output(self):
        with mock.patch("sbxlib.remotecontrol.time.sleep"):
            vm, _ = self.make_vm(["Invalid code\nSBX_LOGIN_EXIT=1"])
            with self.assertRaises(Exception) as ctx:
                remotecontrol.finish_login(vm, "bad")
            self.assertIn("Invalid code", str(ctx.exception))

    def test_enable_writes_settings_seeds_the_dialogs_and_starts_the_unit(self):
        with mock.patch("sbxlib.remotecontrol.time.sleep"):
            vm, seen = self.make_vm(["Enable Remote Control? (y/n)", ""],
                                    logs=["", "Remote Control session: https://claude.ai/code/x"])
            state = remotecontrol.enable(vm, "/home/dev/code/app", "sbx-lab · app", "acceptEdits")
        self.assertEqual(state, "running")
        joined = "\n".join(seen)
        self.assertIn("hasTrustDialogAccepted", joined)
        self.assertIn("systemctl --user enable --now sbx-remote-control", joined)
        self.assertIn("tmux send-keys -t sbx-rc y Enter", joined)

    def test_enable_reports_a_failure_from_the_log(self):
        with mock.patch("sbxlib.remotecontrol.time.sleep"):
            vm, _ = self.make_vm([], logs=["Error: Remote Control requires a full-scope login token."])
            state = remotecontrol.enable(vm, "/home/dev/code/app", "x", "acceptEdits")
        self.assertTrue(state.startswith("failed: Error: Remote Control requires"), state)

    def test_env_file_quotes(self):
        text = remotecontrol.env_file("/home/dev/code/my app", "sbx-x · a", "acceptEdits").decode()
        self.assertIn("RC_DIR='/home/dev/code/my app'", text)
        self.assertIn("RC_NAME='sbx-x · a'", text)


class ProfileTest(unittest.TestCase):
    """sbx new: personal gets the server, agent does not unless asked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name) / "cfg"
        home.mkdir()
        (home / "config.toml").write_text("")
        key = home / "id_ed25519"
        key.write_text("test key material, not a real key")
        key.with_suffix(".pub").write_text("ssh-ed25519 AAAA sbx\n")
        self.patches = [mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(home)}),
                        mock.patch("sbxlib.cli.resolves", return_value=True),
                        mock.patch("sbxlib.cli.dns_has", return_value=True),
                        mock.patch("sbxlib.herdr.available", return_value=False),
                        mock.patch("sbxlib.cli.shutil.which", return_value=None),
                        mock.patch("sbxlib.cli.claudetoken.get", return_value=""),
                        mock.patch("sbxlib.cli._install_cert", return_value=False),
                        mock.patch("sbxlib.cli.remotecontrol.logged_in", return_value=True),
                        mock.patch("sbxlib.cli.remotecontrol.enable", return_value="running")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def run_new(self, *argv):
        from tests.test_cli import FakeApi
        events = []
        return cli.main(["new", *argv], runner=Runner(responder=lambda a, d: ""), api=FakeApi(events))

    def test_the_sign_in_works_without_a_browser_opener(self):
        # A Linux server has neither `open` nor `xdg-open` (shutil.which is
        # None here): the link is printed, and nothing tries to run an opener.
        calls = []
        runner = Runner(responder=lambda a, d: calls.append(a) or "")
        with mock.patch("sbxlib.cli.remotecontrol.logged_in", return_value=False), \
             mock.patch("sbxlib.cli.remotecontrol.start_login", return_value=URL), \
             mock.patch("sbxlib.cli.remotecontrol.finish_login") as finish, \
             mock.patch("sbxlib.cli.sys.stdin") as stdin, \
             mock.patch("sbxlib.cli.getpass.getpass", return_value="the-code"), \
             mock.patch("builtins.print") as out:
            stdin.isatty.return_value = True
            ok = cli._remote_control_setup(cli.load_config(), runner, mock.Mock(), "sbx-p", "personal", None,
                                           "acceptEdits")
        self.assertTrue(ok)
        finish.assert_called_once_with(mock.ANY, "the-code")
        self.assertIn(URL, " ".join(str(c) for c in out.call_args_list))
        self.assertFalse([a for a in calls if a[0] in ("open", "xdg-open")])

    def test_personal_starts_the_server_and_agent_never_does(self):
        enable = cli.remotecontrol.enable
        self.assertEqual(self.run_new("p", "--profile", "personal"), 0)
        enable.assert_called_once()
        self.assertEqual(enable.call_args.args[3], "acceptEdits")
        self.assertEqual(enable.call_args.args[1], "/home/dev/code")
        enable.reset_mock()
        self.assertEqual(self.run_new("a", "--profile", "agent"), 0)
        enable.assert_not_called()
        # No override exists: the flag is refused before any VM is made.
        from tests.test_cli import FakeApi
        events = []
        code = cli.main(["new", "b", "--profile", "agent", "--remote-control-mode", "bypassPermissions"],
                        runner=Runner(responder=lambda a, d: ""), api=FakeApi(events))
        self.assertEqual(code, 1)
        self.assertFalse([e for e in events if e[0] == "api"], "no API call may happen")
        enable.assert_not_called()

    def test_the_module_itself_refuses_an_agent_profile(self):
        with self.assertRaises(Exception) as ctx:
            remotecontrol.check_profile("agent", "sbx-a")
        self.assertIn("not available", str(ctx.exception))
        remotecontrol.check_profile("personal", "sbx-p")

    def test_remote_control_command_refuses_an_agent_sandbox(self):
        from tests.test_cli import FakeApi
        events = []
        api = FakeApi(events, existing=["sbx-a"])  # tagged sbx-agent by FakeApi
        code = cli.main(["remote-control", "a"], runner=Runner(responder=lambda a, d: ""), api=api)
        self.assertEqual(code, 1)
        cli.remotecontrol.enable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
