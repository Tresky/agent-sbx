"""The sandbox key held by an SSH agent: the config paths, the ssh argv, the
SSH block that `_prepare_mac` writes, and the doctor check."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cli, doctor
from sbxlib.config import Config, load
from sbxlib.run import Result, Runner
from sbxlib.vm import Vm

PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA sbx"
OTHER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB work"


class _Home(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "cfg"
        self.home.mkdir()
        self.sock = self.tmp / "agent.sock"
        p = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(self.home)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)


class ConfigPathsTest(_Home):
    def test_without_an_agent_the_identity_is_the_private_file(self):
        cfg = Config()
        self.assertEqual(cfg.ssh_key_path, self.home / "id_ed25519")
        self.assertEqual(cfg.ssh_pubkey_path, self.home / "id_ed25519.pub")
        self.assertEqual(cfg.ssh_identity_path, cfg.ssh_key_path)
        self.assertIsNone(cfg.ssh_agent_path)

    def test_with_an_agent_the_identity_is_the_public_file(self):
        cfg = Config(ssh_agent="~/.1password/agent.sock")
        self.assertEqual(cfg.ssh_identity_path, cfg.ssh_pubkey_path)
        self.assertEqual(cfg.ssh_agent_path, Path("~/.1password/agent.sock").expanduser())

    def test_the_pub_suffix_is_appended_not_swapped(self):
        self.assertEqual(Config(ssh_key="/k/foo.key").ssh_pubkey_path, Path("/k/foo.key.pub"))
        self.assertEqual(Config(ssh_key="/k/foo").ssh_pubkey_path, Path("/k/foo.pub"))
        self.assertEqual(Config(ssh_key="/k/foo.pub").ssh_pubkey_path, Path("/k/foo.pub"))

    def test_config_toml_sets_the_agent(self):
        (self.home / "config.toml").write_text('ssh_agent = "/run/agent.sock"\n')
        self.assertEqual(load().ssh_agent, "/run/agent.sock")


class SshArgvTest(_Home):
    def argv(self, cfg, **kw):
        return Vm(cfg, Runner(), "lab").ssh_argv(**kw)

    def opts(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]

    def test_unchanged_without_an_agent(self):
        argv = self.argv(Config())
        self.assertEqual(argv[argv.index("-i") + 1], str(self.home / "id_ed25519"))
        self.assertFalse([o for o in self.opts(argv) if o.startswith("IdentityAgent")])
        self.assertIn("ForwardAgent=no", self.opts(argv))

    def test_an_agent_replaces_the_private_file_with_the_pub(self):
        argv = self.argv(Config(ssh_agent=str(self.sock)))
        self.assertEqual(argv[argv.index("-i") + 1], str(self.home / "id_ed25519.pub"))
        self.assertIn(f"IdentityAgent={self.sock}", self.opts(argv))
        self.assertIn("IdentitiesOnly=yes", self.opts(argv))
        self.assertNotIn(str(self.home / "id_ed25519"), argv)

    def test_a_socket_path_with_a_space_is_quoted(self):
        argv = self.argv(Config(ssh_agent="/Library/Group Containers/x/agent.sock"))
        self.assertIn('IdentityAgent="/Library/Group Containers/x/agent.sock"', self.opts(argv))

    def test_forwarding_names_the_users_own_agent_not_the_key_agent(self):
        cfg = Config(ssh_agent=str(self.sock))
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/user-agent.sock"}):
            self.assertIn("ForwardAgent=/tmp/user-agent.sock", self.opts(self.argv(cfg, forward_agent=True)))
            self.assertIn("ForwardAgent=no", self.opts(self.argv(cfg)))
            # Without an agent configured, a plain yes is right.
            self.assertIn("ForwardAgent=yes", self.opts(self.argv(Config(), forward_agent=True)))

    def test_forwarding_falls_back_to_yes_without_a_socket(self):
        env = {k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIn("ForwardAgent=yes", self.opts(self.argv(Config(ssh_agent=str(self.sock)), forward_agent=True)))


class PrepareTest(_Home):
    def prepare(self, cfg):
        runner = Runner(responder=lambda argv, data: "")
        with mock.patch("builtins.print"):
            cli._prepare_mac(cfg, runner)
        return runner, (self.home / "ssh_config").read_text()

    def test_it_never_makes_a_key(self):
        runner, block = self.prepare(Config())
        self.assertFalse([c for c in runner.calls if c[0] == "ssh-keygen"])
        self.assertFalse((self.home / "id_ed25519").exists())
        self.assertIn(f"IdentityFile {self.home / 'id_ed25519'}\n", block)
        self.assertNotIn("IdentityAgent", block)

    def test_with_an_agent_the_block_names_the_agent_and_the_pub(self):
        _, block = self.prepare(Config(ssh_agent=str(self.sock)))
        self.assertIn(f"  IdentityFile {self.home / 'id_ed25519.pub'}\n", block)
        self.assertIn(f"  IdentityAgent {self.sock}\n", block)
        self.assertIn("IdentitiesOnly yes", block)

    def test_a_socket_with_a_space_is_quoted_in_the_block(self):
        _, block = self.prepare(Config(ssh_agent="/a b/agent.sock"))
        self.assertIn('  IdentityAgent "/a b/agent.sock"\n', block)

    def test_the_seed_has_a_commented_agent_line_and_the_seed_loads(self):
        self.prepare(Config())
        text = (self.home / "config.toml").read_text()
        self.assertIn("# ssh_agent = ", text)
        self.assertEqual(load().ssh_agent, "")


class DoctorKeyTest(_Home):
    def check(self, cfg, agent_out=None, code=0):
        calls = []

        def responder(argv, data):
            calls.append(argv)
            return Result(code, agent_out or "")

        checks = doctor._key_checks(cfg, Runner(responder=responder))
        self.calls = calls
        return checks

    def cfg(self):
        return Config(ssh_agent=str(self.sock))

    def test_without_an_agent_it_wants_the_private_file(self):
        self.assertEqual(self.check(Config())[0].status, "FAIL")
        (self.home / "id_ed25519").write_text("PRIV")
        self.assertEqual(self.check(Config())[0].status, "ok")

    def test_a_missing_socket_fails(self):
        got = self.check(self.cfg())
        self.assertEqual([c.status for c in got], ["FAIL"])
        self.assertIn("1Password is not running", got[0].detail)

    def test_a_missing_pub_fails(self):
        self.sock.write_text("")
        got = self.check(self.cfg())
        self.assertEqual(got[0].status, "FAIL")
        self.assertIn("id_ed25519.pub", got[0].detail)
        self.assertEqual(self.calls, [])

    def test_a_key_that_the_agent_lists_is_ok(self):
        self.sock.write_text("")
        (self.home / "id_ed25519.pub").write_text(PUB + "\n")
        got = self.check(self.cfg(), f"{OTHER}\n{PUB}\n")
        self.assertEqual([c.status for c in got], ["ok"])
        self.assertEqual(self.calls, [["env", f"SSH_AUTH_SOCK={self.sock}", "ssh-add", "-L"]])

    def test_a_key_that_the_agent_does_not_list_fails(self):
        self.sock.write_text("")
        (self.home / "id_ed25519.pub").write_text(PUB + "\n")
        self.assertEqual(self.check(self.cfg(), OTHER + "\n")[0].status, "FAIL")
        self.assertEqual(self.check(self.cfg(), "", code=1)[0].status, "FAIL")
        got = self.check(self.cfg(), "", code=2)
        self.assertEqual(got[0].status, "FAIL")
        self.assertIn("cannot reach", got[0].detail)

    def test_a_private_file_that_remains_is_a_warning(self):
        self.sock.write_text("")
        (self.home / "id_ed25519.pub").write_text(PUB + "\n")
        (self.home / "id_ed25519").write_text("PRIV")
        got = self.check(self.cfg(), PUB + "\n")
        self.assertEqual([c.status for c in got], ["ok", "WARN"])

    def test_mac_checks_include_it(self):
        with mock.patch("sbxlib.doctor.shutil.which", return_value="/x"), \
                mock.patch("sbxlib.cli._mkcert_root", return_value=Path("/ca")):
            got = doctor.mac_checks(Config(), Runner(responder=lambda a, d: ""))
        self.assertIn("sandbox SSH key", [c.name for c in got])


class ParserTest(unittest.TestCase):
    def test_local_only_and_its_old_name(self):
        parse = cli.build_parser().parse_args
        self.assertTrue(parse(["setup", "--local-only"]).local_only)
        self.assertTrue(parse(["setup", "--mac-only"]).local_only)
        self.assertFalse(parse(["setup"]).local_only)


if __name__ == "__main__":
    unittest.main()
