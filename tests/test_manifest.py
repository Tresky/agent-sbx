import unittest

from sbxlib.manifest import ManifestError, check_git_url, parse, safe_relpath

GOOD = """
[recipe]
setup = ".sandbox/setup.sh"
env_file = ".env.local"

[[input]]
name = "rails-master-key"
kind = "file"
dest = "config/master.key"
secret = true
about = "Decrypts the credentials"

[[input]]
name = "STRIPE_SECRET_KEY"
kind = "env"
required = false
secret = true
placeholder = "sk_test_placeholder"

[[input]]
name = "ui-kit"
kind = "repo"
url = "git@github.com:example/ui-kit.git"
dest = "../ui-kit"
"""


class ParseTest(unittest.TestCase):
    def test_good_manifest(self):
        m = parse(GOOD)
        self.assertEqual(m.env_file, ".env.local")
        self.assertEqual([i.kind for i in m.inputs], ["file", "env", "repo"])
        self.assertTrue(m.inputs[0].required)
        self.assertEqual(m.inputs[1].placeholder, "sk_test_placeholder")
        self.assertEqual(m.inputs[2].dest, "../ui-kit")

    def test_empty_manifest_gets_defaults(self):
        m = parse("")
        self.assertEqual((m.setup, m.env_file, m.inputs), (".sandbox/setup.sh", ".sandbox.env", ()))

    def assert_bad(self, text, fragment):
        with self.assertRaises(ManifestError) as ctx:
            parse(text)
        self.assertIn(fragment, str(ctx.exception))

    def test_unknown_keys_are_errors(self):
        # A typo must not silently drop a restriction such as `agent = false`.
        self.assert_bad('[[input]]\nname="a"\nkind="env"\nagnet=false\n', "unknown key")
        self.assert_bad('source = "x"\n', "unknown top-level")
        self.assert_bad('[recipe]\ncommand = "x"\n', "[recipe] takes only")

    def test_a_manifest_cannot_name_a_source_or_a_command(self):
        for key in ("source", "path", "command", "from"):
            self.assert_bad(f'[[input]]\nname="a"\nkind="file"\ndest="x"\n{key}="/etc/passwd"\n', "unknown key")

    def test_duplicates(self):
        self.assert_bad('[[input]]\nname="A"\nkind="env"\n[[input]]\nname="A"\nkind="env"\n', "appears twice")
        two = '[[input]]\nname="a"\nkind="file"\ndest="x"\n[[input]]\nname="b"\nkind="file"\ndest="./x"\n'
        self.assert_bad(two, "used twice")

    def test_env_name_must_be_an_identifier(self):
        self.assert_bad('[[input]]\nname="MY-VAR"\nkind="env"\n', "env name")

    def test_types(self):
        self.assert_bad('[[input]]\nname="a"\nkind="env"\nsecret="yes"\n', "true or false")
        self.assert_bad('[[input]]\nname="a"\nkind="socket"\n', "'kind' must be")


class PathGuardTest(unittest.TestCase):
    def test_accepts(self):
        self.assertEqual(safe_relpath("config/master.key", "t"), "config/master.key")
        self.assertEqual(safe_relpath("./a/./b", "t"), "a/b")
        self.assertEqual(safe_relpath("../ui-kit", "t", allow_sibling=True), "../ui-kit")

    def test_refuses_every_way_out_of_the_clone(self):
        bad = ["/etc/passwd", "~/.ssh/id_ed25519", "../x", "a/../../x", "a/../b", "", " x", "x ",
               "a\\b", "a\nb", "a\x00b", "-rf", ".git/config", ".", "x" * 300, 7, None]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ManifestError):
                safe_relpath(value, "t")

    def test_sibling_is_exactly_one_level(self):
        for value in ["../../x", "../a/b", "..", "../.."]:
            with self.subTest(value=value), self.assertRaises(ManifestError):
                safe_relpath(value, "t", allow_sibling=True)


class GitUrlTest(unittest.TestCase):
    def test_accepts_plain_remotes(self):
        for url in ["https://github.com/a/b.git", "git@github.com:a/b.git",
                    "ssh://git@gitlab.com:2222/a/b.git", "https://example.com/a/b"]:
            with self.subTest(url=url):
                self.assertEqual(check_git_url(url, "t"), url)

    def test_refuses_option_injection_and_local_transports(self):
        bad = ["--upload-pack=touch /tmp/x", "-oProxyCommand=x", "ext::sh -c id", "file:///etc",
               "/local/path", "http://github.com/a/b", "git@github.com:a/../b", "https://h/a b", ""]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ManifestError):
                check_git_url(url, "t")


class ExamplesTest(unittest.TestCase):
    def test_every_shipped_example_parses(self):
        from pathlib import Path
        found = sorted((Path(__file__).resolve().parent.parent / "examples").glob("*/.sandbox/sandbox.toml"))
        self.assertGreaterEqual(len(found), 1)
        for path in found:
            with self.subTest(path=path.parent.parent.name):
                parse(path.read_text())


if __name__ == "__main__":
    unittest.main()
