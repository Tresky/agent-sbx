import os
import tempfile
import unittest
from pathlib import Path

from sbxlib import inputs
from sbxlib.inputs import PLACEHOLDER, REPO, SEND, SKIP, WITHHELD, InputError
from sbxlib.manifest import parse

MANIFEST = parse("""
[[input]]
name = "key"
kind = "file"
dest = "config/master.key"
secret = true

[[input]]
name = "API_TOKEN"
kind = "env"
required = false
placeholder = "dummy"

[[input]]
name = "seed"
kind = "file"
dest = "db/seed.sql"
required = false

[[input]]
name = "ui"
kind = "repo"
url = "https://github.com/a/ui.git"
dest = "../ui"
""")


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "checkout"
        (self.root / "config").mkdir(parents=True)
        (self.root / "config/master.key").write_text("s3cret")
        self.outside = Path(self.tmp.name) / "outside.txt"
        self.outside.write_text("PRIVATE")

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, profile, **kw):
        return {d.input.name: d for d in inputs.plan(
            MANIFEST, profile, kw.get("checkout", self.root), kw.get("env", {}), kw.get("bindings", {}),
            set(kw.get("with_", [])), set(kw.get("without", [])))}

    def test_personal_sends_what_it_finds(self):
        got = self.plan("personal", env={"API_TOKEN": "abc"})
        self.assertEqual(got["key"].action, SEND)
        self.assertEqual(got["key"].load(), b"s3cret")
        self.assertEqual(got["API_TOKEN"].load(), b"abc")
        self.assertEqual(got["seed"].action, SKIP)
        self.assertEqual(got["ui"].action, REPO)

    def test_personal_uses_the_placeholder_for_a_missing_optional(self):
        got = self.plan("personal")
        self.assertEqual(got["API_TOKEN"].action, PLACEHOLDER)
        self.assertEqual(got["API_TOKEN"].load(), b"dummy")

    def test_agent_needs_an_explicit_decision_for_every_input(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("agent")
        text = str(ctx.exception)
        # All problems at once, including the input the manifest calls non-secret.
        for name in ("key", "API_TOKEN", "seed"):
            self.assertIn(f"--with {name} or --without {name}", text)

    def test_agent_gate_ignores_the_manifest_secret_flag(self):
        # An agent can clear `secret` on its branch; that must not send the file.
        m = parse('[[input]]\nname="key"\nkind="file"\ndest="config/master.key"\nsecret=false\n')
        with self.assertRaises(InputError):
            inputs.plan(m, "agent", self.root, {}, {}, set(), set())

    def test_agent_with_and_without(self):
        got = self.plan("agent", with_=["key"], without=["API_TOKEN", "seed"])
        self.assertEqual(got["key"].action, SEND)
        self.assertEqual(got["API_TOKEN"].action, PLACEHOLDER)
        self.assertEqual(got["seed"].action, WITHHELD)

    def test_agent_false_cannot_be_overridden(self):
        m = parse('[[input]]\nname="key"\nkind="file"\ndest="config/master.key"\nagent=false\n')
        with self.assertRaises(InputError) as ctx:
            inputs.plan(m, "agent", self.root, {}, {}, {"key"}, set())
        self.assertIn("agent = false", str(ctx.exception))
        self.assertEqual(inputs.plan(m, "agent", self.root, {}, {}, set(), set())[0].action, WITHHELD)
        self.assertEqual(inputs.plan(m, "personal", self.root, {}, {}, set(), set())[0].action, SEND)

    def test_flag_errors(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("personal", with_=["nope"], without=["key", "nope2"])
        self.assertIn("names no input: nope", str(ctx.exception))
        with self.assertRaises(InputError) as ctx:
            self.plan("personal", with_=["key"], without=["key"])
        self.assertIn("both --with and --without", str(ctx.exception))

    def test_required_and_missing_is_an_error_before_any_vm(self):
        (self.root / "config/master.key").unlink()
        with self.assertRaises(InputError) as ctx:
            self.plan("personal")
        self.assertIn("key: required", str(ctx.exception))

    def test_symlink_is_refused_not_followed(self):
        # The attack: a branch commits config/master.key -> a file outside.
        (self.root / "config/master.key").unlink()
        os.symlink(self.outside, self.root / "config/master.key")
        with self.assertRaises(InputError) as ctx:
            self.plan("personal")
        self.assertIn("symlink", str(ctx.exception))

    def test_symlinked_directory_is_refused(self):
        (self.root / "config/master.key").unlink()
        (self.root / "config").rmdir()
        os.symlink(self.outside.parent, self.root / "config")
        (self.outside.parent / "master.key").write_text("PRIVATE")
        with self.assertRaises(InputError) as ctx:
            self.plan("personal")
        self.assertIn("symlink", str(ctx.exception))

    def test_controlled_pair_regular_file_passes_where_symlink_fails(self):
        # Same path, same plan call; only the file type differs.
        self.assertEqual(self.plan("personal")["key"].action, SEND)

    def test_binding_command_and_path(self):
        bindings = {"key": {"command": ["printf", "from-cmd"]}, "API_TOKEN": {"path": str(self.outside)}}
        got = self.plan("personal", bindings=bindings)
        self.assertEqual(got["key"].load(), b"from-cmd")
        self.assertEqual(got["API_TOKEN"].load(), b"PRIVATE")

    def test_bindings_file_validation(self):
        path = Path(self.tmp.name) / "b.toml"
        path.write_text('[inputs.key]\ncommand = "op read x"\n')
        with self.assertRaises(InputError):
            inputs.load_bindings(path)
        path.write_text('[inputs.key]\ncommand = ["op", "read", "x"]\npath = "y"\n')
        with self.assertRaises(InputError):
            inputs.load_bindings(path)
        path.write_text('[inputs.key]\ncommand = ["op", "read", "x"]\n')
        self.assertEqual(inputs.load_bindings(path).inputs["key"]["command"][0], "op")

    def test_git_section(self):
        path = Path(self.tmp.name) / "g.toml"
        path.write_text('[git]\ntoken_command = ["security", "find-generic-password", "-s", "sbx-git-myapp", "-w"]\n')
        git = inputs.load_bindings(path).git
        self.assertEqual((git.host, git.username, git.token_command[-2]), ("github.com", "x-access-token", "sbx-git-myapp"))
        path.write_text('[git]\ntoken_command = ["x"]\nhost = "gitlab.com"\nusername = "oauth2"\n')
        self.assertEqual(inputs.load_bindings(path).git.host, "gitlab.com")
        for bad in ('[git]\nhost = "x"\n', '[git]\ntoken_command = "op read x"\n', '[git]\ntoken_command = ["x"]\nrepo = "y"\n',
                    '[gti]\ntoken_command = ["x"]\n'):
            path.write_text(bad)
            with self.subTest(bad=bad), self.assertRaises(InputError):
                inputs.load_bindings(path)
        self.assertIsNone(inputs.load_bindings(Path(self.tmp.name) / "absent.toml").git)

    def test_preview_never_raises_on_a_refused_source(self):
        (self.root / "config/master.key").unlink()
        os.symlink(self.outside, self.root / "config/master.key")
        rows = inputs.preview(MANIFEST, self.root, {}, {})
        self.assertIn("REFUSED", rows[0].state)
        self.assertIn("key", inputs.table(rows))


if __name__ == "__main__":
    unittest.main()
