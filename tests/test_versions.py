"""`sbx versions`: what a checkout declares, the union over projects, and the
local.conf lines that the template build reads."""
import tempfile
import unittest
from pathlib import Path

from sbxlib import projects, versions


def make_checkout(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class ScanTest(unittest.TestCase):
    def test_a_rails_checkout_with_a_go_service_and_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            make_checkout(root, {
                ".ruby-version": "ruby-3.3.6\n",
                ".nvmrc": "22\n",
                "docker-compose.yml": "services:\n  db:\n    image: postgis/postgis:15-3.3\n  redis:\n    image: 'redis:6.0.13'\n  app:\n    build: .\n",
                "services/api/go.mod": "module x\n\ngo 1.25.0\n\nrequire github.com/gorilla/mux v1.8.1\n",
                "services/auth/docker-compose.yml": "services:\n  kc-db:\n    image: postgres:16\n  kc:\n    build: .\n",
                "node_modules/junk/docker-compose.yml": "services:\n  x:\n    image: should-not-be-seen:1\n",
                "env/pyvenv.cfg": "home = /usr/bin\n",
            })
            needs = versions.Needs()
            versions.scan_checkout(root, "app", needs)
        self.assertEqual(needs.ruby, {"3.3.6": {"app"}})
        self.assertEqual(needs.node, {"22": {"app"}})
        self.assertEqual(needs.go, {"1.25.0": {"app"}})
        self.assertEqual(set(needs.images), {"postgis/postgis:15-3.3", "redis:6.0.13", "postgres:16"})

    def test_gemfile_ruby_and_node_version_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "lib"
            make_checkout(root, {"Gemfile": "source 'https://rubygems.org'\nruby '3.2.4'\n", ".node-version": "v22.21.0\n"})
            needs = versions.Needs()
            versions.scan_checkout(root, "lib", needs)
        self.assertEqual((needs.ruby, needs.node), ({"3.2.4": {"lib"}}, {"22.21.0": {"lib"}}))

    def test_union_over_the_registry_and_the_conf_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            make_checkout(a, {".ruby-version": "3.3.6\n", ".nvmrc": "22\n"})
            make_checkout(b, {".ruby-version": "3.4.10\n", ".nvmrc": "22\n", "compose.yaml": "services:\n  r:\n    image: redis:7\n"})
            registry = {"a": projects.Entry("a", str(a), ""), "b": projects.Entry("b", str(b), ""),
                        "gone": projects.Entry("gone", str(Path(tmp) / "nope"), "https://x/y.git")}
            needs, missing = versions.scan_registry(registry)
        self.assertEqual(missing, ["gone"])
        self.assertEqual(needs.node, {"22": {"a", "b"}})
        lines = versions.conf_lines(needs)
        # Oldest first: the first entry becomes the template's default.
        self.assertEqual(lines["SBX_RUBY_VERSIONS"], "3.3.6 3.4.10")
        self.assertEqual(lines["SBX_NODE_VERSIONS"], "22")
        self.assertEqual(lines["SBX_DOCKER_IMAGES"], "redis:7")
        text = versions.table(needs, {"3.4.10"}, set(), set())
        self.assertRegex(text, r"ruby\s+3\.3\.6\s+NO\s+a")
        self.assertRegex(text, r"ruby\s+3\.4\.10\s+yes\s+b")

    def test_write_local_conf_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.conf"
            path.write_text('# mine\nSBX_LAN_BRIDGE=vmbr1\nSBX_RUBY_VERSIONS="3.0.0"\n')
            versions.write_local_conf(path, {"SBX_RUBY_VERSIONS": "3.3.6 3.4.10", "SBX_NODE_VERSIONS": "22", "SBX_DOCKER_IMAGES": ""})
            text = path.read_text()
        self.assertIn("# mine\nSBX_LAN_BRIDGE=vmbr1\n", text)
        self.assertEqual(text.count("SBX_RUBY_VERSIONS"), 1)
        self.assertIn('SBX_RUBY_VERSIONS="3.3.6 3.4.10"', text)
        self.assertIn('SBX_NODE_VERSIONS="22"', text)
        # The bash side reads the same value back.
        from sbxlib.config import parse_env_file
        self.assertEqual(parse_env_file(text)["SBX_RUBY_VERSIONS"], "3.3.6 3.4.10")


if __name__ == "__main__":
    unittest.main()
