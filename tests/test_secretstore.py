import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import claudetoken, secretstore
from sbxlib.run import Runner


def _no_commands(argv, data):
    raise AssertionError(f"the file store ran a command: {argv}")


class FileStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"SBX_SECRET_STORE": "file", "SBX_CONFIG_DIR": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.runner = Runner(responder=_no_commands)

    def test_store_get_forget(self):
        self.assertFalse(secretstore.exists(self.runner, "sbx-pve-token"))
        self.assertEqual(secretstore.get(self.runner, "sbx-pve-token"), "")
        secretstore.store(self.runner, "sbx-pve-token", "sbx@pve!cli=secret")
        self.assertTrue(secretstore.exists(self.runner, "sbx-pve-token"))
        self.assertEqual(secretstore.get(self.runner, "sbx-pve-token"), "sbx@pve!cli=secret")
        secretstore.store(self.runner, "sbx-pve-token", "sbx@pve!cli=new")
        self.assertEqual(secretstore.get(self.runner, "sbx-pve-token"), "sbx@pve!cli=new")
        self.assertTrue(secretstore.forget(self.runner, "sbx-pve-token"))
        self.assertFalse(secretstore.forget(self.runner, "sbx-pve-token"))

    def test_only_this_user_can_read_it(self):
        secretstore.store(self.runner, "sbx-claude-token", "sk-ant-oat01-x")
        d = self.home / "secrets"
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((d / "sbx-claude-token").stat().st_mode), 0o600)
        self.assertEqual([p.name for p in d.iterdir()], ["sbx-claude-token"])  # no temporary file left

    def test_the_command_prints_the_secret(self):
        # config.toml and the bindings name this command; it must work on its own.
        secretstore.store(self.runner, "sbx-git-myapp", "ghp_token")
        done = subprocess.run(secretstore.command("sbx-git-myapp"), capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout.strip(), "ghp_token")

    def test_the_claude_token_uses_it(self):
        claudetoken.store(self.runner, "sk-ant-oat01-y")
        self.assertEqual(claudetoken.get(self.runner), "sk-ant-oat01-y")
        self.assertTrue(claudetoken.forget(self.runner))
        self.assertEqual(claudetoken.get(self.runner), "")


class KindTest(unittest.TestCase):
    def test_the_platform_picks_the_store(self):
        with mock.patch.dict(os.environ, {"SBX_SECRET_STORE": ""}):
            with mock.patch.object(secretstore.sys, "platform", "darwin"):
                self.assertEqual(secretstore.kind(), "keychain")
            with mock.patch.object(secretstore.sys, "platform", "linux"):
                self.assertEqual(secretstore.kind(), "file")

    def test_a_wrong_value_is_an_error(self):
        with mock.patch.dict(os.environ, {"SBX_SECRET_STORE": "vault"}):
            with self.assertRaises(ValueError):
                secretstore.kind()

    def test_the_keychain_command(self):
        self.assertEqual(secretstore.command("sbx-claude-token", "sbx"),
                         ["security", "find-generic-password", "-s", "sbx-claude-token", "-a", "sbx", "-w"])


if __name__ == "__main__":
    unittest.main()
