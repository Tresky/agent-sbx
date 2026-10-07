"""The `op` CLI and the SSH agent listing: the argv of each call and the
parsing of what comes back. Nothing here runs op or ssh-add."""
import json
import unittest
from unittest import mock

from sbxlib import onepassword as op
from sbxlib.run import Result, Runner

KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA sbx"
KEY_A_OTHER_COMMENT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA laptop"
KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB work"
RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQ comment with spaces"


def runner_for(result):
    """A runner whose every call returns `result`; `.calls` holds the argv."""
    return Runner(responder=lambda argv, data: result)


class KeyLineTest(unittest.TestCase):
    def test_fields_are_the_type_and_the_base64(self):
        self.assertEqual(op.key_fields(RSA), ("ssh-rsa", "AAAAB3NzaC1yc2EAAAADAQABAAABgQ"))
        self.assertIsNone(op.key_fields(""))
        self.assertIsNone(op.key_fields("no key here"))
        self.assertIsNone(op.key_fields("ssh-ed25519"))

    def test_the_comment_does_not_matter(self):
        self.assertTrue(op.same_key(KEY_A, KEY_A_OTHER_COMMENT))
        self.assertTrue(op.same_key(KEY_A, KEY_A.rsplit(" ", 1)[0]))
        self.assertFalse(op.same_key(KEY_A, KEY_B))
        self.assertFalse(op.same_key("", ""))

    def test_lists_key(self):
        self.assertTrue(op.lists_key([KEY_B, KEY_A_OTHER_COMMENT], KEY_A + "\n"))
        self.assertFalse(op.lists_key([KEY_B], KEY_A))
        self.assertFalse(op.lists_key([], KEY_A))


class AgentKeysTest(unittest.TestCase):
    def test_argv_sets_the_socket_for_this_call_only(self):
        runner = runner_for(Result(0, f"{KEY_A}\n{KEY_B}\n"))
        self.assertEqual(op.agent_keys(runner, "/tmp/agent.sock"), [KEY_A, KEY_B])
        self.assertEqual(runner.calls, [["env", "SSH_AUTH_SOCK=/tmp/agent.sock", "ssh-add", "-L"]])

    def test_exit_1_is_an_agent_with_no_keys(self):
        self.assertEqual(op.agent_keys(runner_for(Result(1, "The agent has no identities.\n")), "/s"), [])

    def test_exit_2_is_an_agent_that_cannot_be_reached(self):
        with self.assertRaisesRegex(op.OpError, "cannot reach the agent at /s"):
            op.agent_keys(runner_for(Result(2, "", "Could not open a connection")), "/s")

    def test_lines_that_are_not_keys_are_dropped(self):
        self.assertEqual(op.agent_keys(runner_for(Result(0, f"\nnoise\n{KEY_A}\n")), "/s"), [KEY_A])


class OpCliTest(unittest.TestCase):
    def test_installed_follows_the_path(self):
        with mock.patch("sbxlib.onepassword.shutil.which", return_value=None):
            self.assertFalse(op.installed())
            self.assertFalse(op.available(runner_for(Result(0))))
        with mock.patch("sbxlib.onepassword.shutil.which", return_value="/usr/bin/op"):
            self.assertTrue(op.available(runner_for(Result(0))))
            self.assertFalse(op.available(runner_for(Result(1))))

    def test_version_reads_op_version(self):
        runner = runner_for(Result(0, "2.30.0\n"))
        self.assertEqual(op.version(runner), (2, 30, 0))
        self.assertEqual(runner.calls, [["op", "--version"]])
        self.assertEqual(op.version(runner_for(Result(0, "2.31.0-beta.01\n"))), (2, 31, 0))
        self.assertIsNone(op.version(runner_for(Result(0, "nonsense\n"))))
        self.assertIsNone(op.version(runner_for(Result(1, "2.30.0\n"))))

    def test_too_old_is_below_the_first_op_with_ssh_key_items(self):
        self.assertEqual(op.too_old(runner_for(Result(0, "2.6.1\n"))), "2.6.1")
        self.assertEqual(op.too_old(runner_for(Result(0, "2.19.9\n"))), "2.19.9")
        self.assertEqual(op.too_old(runner_for(Result(0, "2.20.0\n"))), "")
        self.assertEqual(op.too_old(runner_for(Result(0, "3.0.0\n"))), "")
        self.assertEqual(op.too_old(runner_for(Result(1))), "")

    def test_signed_in_runs_whoami(self):
        runner = runner_for(Result(0))
        self.assertTrue(op.signed_in(runner))
        self.assertEqual(runner.calls, [["op", "whoami"]])

    def test_create_reads_only_the_ids_and_never_asks_to_reveal(self):
        out = json.dumps({"id": "item1", "title": "sbx", "vault": {"id": "vault1", "name": "Private"},
                          "fields": [{"id": "private_key", "value": "-----BEGIN PRIVATE KEY-----DUMMY"}]})
        runner = runner_for(Result(0, out))
        ref = op.create_ssh_key(runner, "sbx", "")
        self.assertEqual(ref, op.ItemRef("vault1", "item1"))
        self.assertEqual(runner.calls, [["op", "item", "create", "--category", "SSH Key", "--title", "sbx",
                                         "--ssh-generate-key", "ed25519", "--format", "json"]])
        self.assertNotIn("--reveal", runner.calls[0])

    def test_create_in_a_vault(self):
        out = json.dumps({"id": "i", "vault": {"id": "v"}})
        runner = runner_for(Result(0, out))
        op.create_ssh_key(runner, "sbx", "Work")
        argv = runner.calls[0]
        self.assertEqual(argv[argv.index("--vault") + 1], "Work")

    def test_create_errors_never_carry_the_output(self):
        with self.assertRaises(op.OpError) as got:
            op.create_ssh_key(runner_for(Result(0, "-----BEGIN PRIVATE KEY-----DUMMY not json")), "sbx")
        self.assertNotIn("DUMMY", str(got.exception))
        with self.assertRaisesRegex(op.OpError, "no item id"):
            op.create_ssh_key(runner_for(Result(0, json.dumps({"id": "i"}))), "sbx")

    def test_a_failing_op_becomes_an_op_error_with_its_stderr(self):
        with self.assertRaisesRegex(op.OpError, "not signed in"):
            op.create_ssh_key(runner_for(Result(1, "", "[ERROR] you are not signed in")), "sbx")

    def test_public_key_reads_the_reference(self):
        runner = runner_for(Result(0, KEY_A + "\n"))
        self.assertEqual(op.public_key(runner, op.ItemRef("v1", "i1")), KEY_A)
        self.assertEqual(runner.calls, [["op", "read", "op://v1/i1/public key"]])
        with self.assertRaisesRegex(op.OpError, "no public key"):
            op.public_key(runner_for(Result(0, "\n")), op.ItemRef("v1", "i1"))

    def test_find_matches_the_title_exactly(self):
        items = [{"id": "a", "title": "sbx-old", "vault": {"id": "v1"}},
                 {"id": "b", "title": "sbx", "vault": {"id": "v2"}}]
        runner = runner_for(Result(0, json.dumps(items)))
        self.assertEqual(op.find_ssh_key(runner, "sbx"), op.ItemRef("v2", "b"))
        self.assertEqual(runner.calls, [["op", "item", "list", "--categories", "SSH Key", "--format", "json"]])
        self.assertIsNone(op.find_ssh_key(runner_for(Result(0, "[]")), "sbx"))
        self.assertIsNone(op.find_ssh_key(runner_for(Result(0, "")), "sbx"))

    def test_default_socket_depends_on_the_system(self):
        with mock.patch("sbxlib.onepassword.sys.platform", "darwin"):
            self.assertIn("2BUA8C4S2C.com.1password", op.default_socket())
        with mock.patch("sbxlib.onepassword.sys.platform", "linux"):
            self.assertEqual(op.default_socket(), "~/.1password/agent.sock")


if __name__ == "__main__":
    unittest.main()
