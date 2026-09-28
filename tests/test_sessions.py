"""sbx autostart: which sessions are live, what a boot resumes, and that
turning it on touches configuration and files only, never a running VM."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cli, sessions
from sbxlib.pve import AUTOSTART_TAG, Pve, Sandbox
from sbxlib.run import Runner


def own_start_time() -> str:
    with open(f"/proc/{os.getpid()}/stat") as fh:
        return fh.read().rsplit(")", 1)[1].split()[19]


def session_file(home: Path, pid: int, sid: str, start: str, fname: str = "", **extra) -> None:
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / (fname or f"{pid}.json")).write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": str(home / "code" / "app"),
                                               "procStart": start, "kind": "interactive", "name": "app-1",
                                               **extra}))


class LivenessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.ns = {}
        exec(sessions._LIVE, self.ns)

    def test_live_stale_and_reused(self):
        me, start = os.getpid(), own_start_time()
        session_file(self.home, me, "live-1", start, bridgeSessionId="session_x")
        session_file(self.home, me + 10**7, "stale-2", start)             # no such process
        session_file(self.home, 1, "reused-3", "1")                      # pid 1, another start time
        session_file(self.home, me, "print-4", start, fname="p.json", kind="print")  # not interactive
        (self.home / ".claude/sessions/9.json").write_text("{not json")   # unreadable
        live = self.ns["live_sessions"](str(self.home))
        self.assertEqual([s["sessionId"] for s in live], ["live-1"])
        self.assertTrue(live[0]["remote"])
        self.assertEqual(live[0]["cwd"], str(self.home / "code" / "app"))


class ProgramsTest(unittest.TestCase):
    """The two programs, run for real against a temporary HOME."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name, text in (("sbx-claude-track", sessions.TRACKER), ("sbx-claude-resume", sessions.RESUMER)):
            (self.bin / name).write_text(text)
        self.tmux_log = self.home / "tmux.log"
        (self.bin / "tmux").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {self.tmux_log}\necho ---- >> {self.tmux_log}\n")
        (self.bin / "tmux").chmod(0o755)

    def run_prog(self, name, *args, logged_in=True):
        body = '{"loggedIn": true, "authMethod": "claude.ai"}' if logged_in else '{"loggedIn": false}'
        (self.bin / "claude").write_text(f"#!/bin/sh\necho '{body}'\n")
        (self.bin / "claude").chmod(0o755)
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:{os.environ['PATH']}", "SBX_RESUME_NO_WAIT": "1",
               "ANTHROPIC_BASE_URL": "http://10.79.0.1:8080", "ANTHROPIC_AUTH_TOKEN": "placeholder"}
        return subprocess.run([sys.executable, str(self.bin / name), *args], env=env,
                              capture_output=True, text=True, check=True)

    def test_the_tracker_records_the_live_sessions(self):
        session_file(self.home, os.getpid(), "live-1", own_start_time())
        session_file(self.home, os.getpid() + 10**7, "gone-2", "5")
        self.assertIn("live-1", self.run_prog("sbx-claude-track", "--print").stdout)
        self.assertFalse((self.home / sessions.STATE_FILE).exists(), "--print writes nothing")
        self.run_prog("sbx-claude-track")
        recorded = json.loads((self.home / sessions.STATE_FILE).read_text())
        self.assertEqual([s["sessionId"] for s in recorded], ["live-1"])

    def write_state(self, *ids):
        state = self.home / sessions.STATE_FILE
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps([{"sessionId": i, "cwd": str(self.home), "name": "app 1", "remote": True}
                                     for i in ids]))

    def test_the_resumer_starts_what_is_not_live_on_remote_control(self):
        session_file(self.home, os.getpid(), "live-1", own_start_time())
        self.write_state("live-1", "gone-2222")
        self.run_prog("sbx-claude-resume")
        calls = self.tmux_log.read_text().split("----\n")[:-1]
        self.assertEqual(len(calls), 1, "a live session is not started twice")
        args = calls[0].splitlines()
        self.assertEqual(args[:2], ["-L", sessions.TMUX_SOCKET])
        self.assertIn("resume-gone-222", args)
        cmd = args[-1]
        self.assertIn("claude --resume gone-2222 --remote-control 'app 1'", cmd)
        for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            self.assertIn(f"-u {var}", cmd)
        self.assertIn("--permission-mode acceptEdits", cmd)
        self.assertIn("live already", (self.home / sessions.RESUME_LOG).read_text())

    def test_without_the_full_login_it_resumes_in_a_terminal(self):
        self.write_state("gone-3333")
        self.run_prog("sbx-claude-resume", logged_in=False)
        cmd = self.tmux_log.read_text().split("----\n")[0].splitlines()[-1]
        self.assertEqual(cmd, "claude --resume gone-3333")

    def test_nothing_recorded_starts_nothing(self):
        self.run_prog("sbx-claude-resume")
        self.assertFalse(self.tmux_log.exists())


class AutostartCommandTest(unittest.TestCase):
    """The hard rule: turning it on or off never stops, restarts, rolls back or
    removes anything. Only the allowed Pve methods may be called."""
    ALLOWED = {"require", "set_autostart", "sidecars"}

    def run_cmd(self, *argv):
        pve = mock.create_autospec(Pve, instance=True)
        pve.require.return_value = Sandbox(9107, "sbx-app", "pve", "running", ("sbx", "sbx-agent"))
        ssh = []
        runner = Runner(responder=lambda a, d: ssh.append(a[-1]) or "[]")
        with mock.patch("sbxlib.cli._pve", return_value=pve):
            code = cli.main(["autostart", "app", *argv], runner=runner)
        self.assertEqual(code, 0)
        called = {c[0].split(".")[0] for c in pve.method_calls}
        self.assertLessEqual(called, self.ALLOWED, f"unexpected calls: {called - self.ALLOWED}")
        return pve, ssh

    def test_on_marks_the_vms_and_enables_without_starting_a_resume(self):
        pve, ssh = self.run_cmd()
        pve.set_autostart.assert_called_once_with(pve.require.return_value, True)
        joined = "\n".join(ssh)
        self.assertIn("systemctl --user enable sbx-claude-resume.service", joined)
        self.assertNotIn("enable --now sbx-claude-resume", joined)
        self.assertNotIn("start sbx-claude-resume", joined)
        self.assertIn("enable --now sbx-claude-track.timer", joined)
        for word in ("reboot", "poweroff", "kill", "restart"):
            self.assertNotIn(word, joined)

    def test_off_and_status(self):
        pve, _ = self.run_cmd("--off")
        pve.set_autostart.assert_called_once_with(pve.require.return_value, False)
        pve, _ = self.run_cmd("--status")
        pve.set_autostart.assert_not_called()


class PveTest(unittest.TestCase):
    def pve(self, sidecars):
        calls = []
        pve = Pve.__new__(Pve)
        pve.api = lambda method, path, body=None: calls.append((method, path, body))
        pve.sidecars = lambda: sidecars
        return pve, calls

    def test_set_autostart_sets_onboot_on_both_and_no_start_order(self):
        box = Sandbox(9107, "sbx-app", "pve", "running", ("sbx", "sbx-agent", "sbx-exp-20261001"))
        sc = Sandbox(9108, "sbx-app-sc", "pve", "running", ("sbx-sidecar",))
        pve, calls = self.pve({"sbx-app": sc})
        pve.set_autostart(box, True)
        self.assertEqual([(m, p.split("/qemu/")[1]) for m, p, _ in calls], [("PUT", "9108/config"), ("PUT", "9107/config")])
        # No `startup`: a start order needs Sys.Modify on the whole host.
        self.assertEqual(calls[0][2], {"onboot": 1})
        self.assertEqual(calls[1][2], {"onboot": 1, "tags": f"sbx;sbx-agent;sbx-exp-20261001;{AUTOSTART_TAG}"})
        pve.set_autostart(Sandbox(9107, "sbx-app", "pve", "running", ("sbx", AUTOSTART_TAG)), False)
        self.assertEqual(calls[-1][2], {"onboot": 0, "tags": "sbx"})

    def test_snap_covers_the_sidecar_and_ram(self):
        box = Sandbox(9107, "sbx-app", "pve", "running", ("sbx",))
        sc = Sandbox(9108, "sbx-app-sc", "pve", "running", ("sbx-sidecar",))
        pve = mock.create_autospec(Pve, instance=True)
        pve.require.return_value = box
        pve.sidecars.return_value = {"sbx-app": sc}
        with mock.patch("sbxlib.cli._pve", return_value=pve):
            self.assertEqual(cli.main(["snap", "app", "before", "--ram"], runner=Runner(responder=lambda a, d: "")), 0)
        self.assertEqual([c.args[1] for c in pve.snapshot.call_args_list], [9107, 9108])
        self.assertTrue(all(c.kwargs["ram"] for c in pve.snapshot.call_args_list))


if __name__ == "__main__":
    unittest.main()
