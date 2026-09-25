"""`sbx template rebuild`: the order of its steps, the refusal while linked
clones exist, and that the host script gets the confirmation it needs."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cli
from sbxlib.run import Runner
from tests.test_cli import FakeApi


class RebuildTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name) / "cfg"
        home.mkdir()
        (home / "config.toml").write_text('pve_api = "https://192.0.2.10:8006"\n')
        self.patches = [mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(home)}),
                        mock.patch("sbxlib.herdr.available", return_value=False),
                        mock.patch("sbxlib.cli.versions_mod.scan_registry", return_value=(mock.MagicMock(ruby={}, node={}, images={}), []))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def run_cmd(self, *argv, existing=()):
        events = []
        api = FakeApi(events, existing=existing)
        runner = Runner(responder=lambda a, d: events.append(("cmd", " ".join(a))) or "")
        return cli.main(["template", *argv], runner=runner, api=api), events

    def test_rebuild_copies_then_builds_with_the_confirmation_passed_through(self):
        code, events = self.run_cmd("rebuild", "-y")
        self.assertEqual(code, 0)
        cmds = [e[1] for e in events if e[0] == "cmd"]
        scp = next(i for i, c in enumerate(cmds) if c.startswith("scp "))
        build = next(i for i, c in enumerate(cmds) if "30-template-build.sh --replace" in c)
        self.assertLess(scp, build)
        self.assertIn("root@192.0.2.10:/root/sbx/", cmds[scp])
        self.assertIn("PubkeyAuthentication=no", cmds[scp])
        self.assertIn("ControlMaster=auto", cmds[build])
        self.assertIn("-t root@192.0.2.10 SBX_YES=1 bash", cmds[build])

    def test_rebuild_refuses_while_linked_clones_exist(self):
        code, events = self.run_cmd("rebuild", "-y", existing=["sbx-old"])
        self.assertEqual(code, 1)
        self.assertFalse([e for e in events if e[0] == "cmd" and e[1].startswith("scp")])

    def test_rm_sandboxes_destroys_them_before_the_build(self):
        code, events = self.run_cmd("rebuild", "-y", "--rm-sandboxes", existing=["sbx-old"])
        self.assertEqual(code, 0)
        deleted = next(i for i, e in enumerate(events) if e[0] == "api" and e[1] == "DELETE")
        scp = next(i for i, e in enumerate(events) if e[0] == "cmd" and e[1].startswith("scp"))
        self.assertLess(deleted, scp)

    def test_status_and_finish(self):
        code, events = self.run_cmd("status")
        self.assertEqual(code, 0)
        code, events = self.run_cmd("finish")
        self.assertEqual(code, 0)
        self.assertTrue(any("30-template-build.sh --finish" in e[1] for e in events if e[0] == "cmd"))


if __name__ == "__main__":
    unittest.main()
