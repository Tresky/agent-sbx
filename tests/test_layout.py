import json
import unittest

from sbxlib import layout
from sbxlib.run import Result


MYAPP = """
tab = "myapp"
[[row]]
height = 0.5
panes  = ["rails", "compose", "auth"]
widths = [0.5, 0.25, 0.25]
[[row]]
height = 0.5
panes  = ["vite", "go", ["ui-lib", "ui-vue"], "caddy"]
widths = [0.25, 0.25, 0.25, 0.25]
[pane.rails]
run = "bin/rails s"
[pane.auth]
cwd = "services/auth"
run = "docker compose up"
[pane.ui-lib]
cwd = "../ui-lib/packages/core"
run = "yarn build:watch"
"""


class Parse(unittest.TestCase):
    def test_grid(self):
        lay = layout.parse(MYAPP)
        self.assertEqual(lay.tab, "myapp")
        self.assertEqual([r.height for r in lay.rows], [0.5, 0.5])
        self.assertEqual(lay.rows[1].cells, [["vite"], ["go"], ["ui-lib", "ui-vue"], ["caddy"]])
        self.assertEqual(lay.panes["auth"].cwd, "services/auth")
        self.assertEqual(lay.panes["ui-lib"].cwd, "../ui-lib/packages/core")
        self.assertEqual(lay.panes["compose"].run, "")      # a pane with no table: a shell
        self.assertEqual(len(lay.names), 8)

    def test_defaults_share_equally(self):
        lay = layout.parse('[[row]]\npanes = ["a", "b"]\n[[row]]\npanes = ["c"]\n')
        self.assertEqual(lay.tab, "dev")
        self.assertEqual([r.height for r in lay.rows], [0.5, 0.5])
        self.assertEqual(lay.rows[0].widths, [0.5, 0.5])
        self.assertEqual(lay.rows[1].widths, [1.0])

    def test_refusals(self):
        bad = {
            "widths must add up": '[[row]]\npanes = ["a", "b"]\nwidths = [0.5, 0.2]\n',
            "widths must match": '[[row]]\npanes = ["a", "b"]\nwidths = [1.0]\n',
            "a pane twice": '[[row]]\npanes = ["a", "a"]\n',
            "an unknown pane table": '[[row]]\npanes = ["a"]\n[pane.b]\nrun = "x"\n',
            "an unknown pane key": '[[row]]\npanes = ["a"]\n[pane.a]\nshell = "x"\n',
            "an absolute cwd": '[[row]]\npanes = ["a"]\n[pane.a]\ncwd = "/etc"\n',
            "a home cwd": '[[row]]\npanes = ["a"]\n[pane.a]\ncwd = "~/x"\n',
            "a cwd two levels up": '[[row]]\npanes = ["a"]\n[pane.a]\ncwd = "../../x"\n',
            "no rows": 'tab = "x"\n',
            "an unknown top key": '[[row]]\npanes = ["a"]\nfocus = "a"\n',
        }
        for what, text in bad.items():
            with self.subTest(what):
                with self.assertRaises(layout.LayoutError):
                    layout.parse(text)

    def test_sibling_cwd_is_allowed(self):
        lay = layout.parse('[[row]]\npanes = ["a"]\n[pane.a]\ncwd = "../ui-lib/caddy"\n')
        self.assertEqual(lay.panes["a"].cwd, "../ui-lib/caddy")


class FakeHerdr:
    """Answers `herdr --machine X ...` the way the real one does, and records
    every call. Panes get ids in order of creation; the layout it reports
    follows the splits, so `check` can be tested too."""

    def __init__(self, tabs=()):
        self.calls = []
        self.next_pane = 1
        self.tabs = list(tabs)
        self.rects = {}          # pane id -> (x, y, w, h) in a 1000 x 1000 area

    def run(self, argv, *, input=None, check=True, capture=True, timeout=None):
        assert argv[:3] == ["herdr", "--machine", "sbx-x"], argv
        args = argv[3:]
        self.calls.append(args)
        return Result(0, json.dumps({"result": self.answer(args)}), "")

    def answer(self, args):
        group, verb = args[0], args[1]
        if (group, verb) == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "w1", "focused": True}]}
        if (group, verb) == ("tab", "list"):
            return {"tabs": self.tabs}
        if (group, verb) == ("tab", "close"):
            self.tabs = [t for t in self.tabs if t["tab_id"] != args[2]]
            return {"type": "ok"}
        if (group, verb) == ("tab", "create"):
            pid = self.new_pane()
            self.rects[pid] = (0, 0, 1000, 1000)
            return {"tab": {"tab_id": "w1:t9"}, "root_pane": {"pane_id": pid}}
        if (group, verb) == ("pane", "split"):
            src = args[2]
            opts = dict(zip(args[3::2], args[4::2]))
            ratio = float(opts["--ratio"])
            x, y, w, h = self.rects[src]
            pid = self.new_pane()
            if opts["--direction"] == "down":
                keep = round(h * ratio)
                self.rects[src] = (x, y, w, keep)
                self.rects[pid] = (x, y + keep, w, h - keep)
            else:
                keep = round(w * ratio)
                self.rects[src] = (x, y, keep, h)
                self.rects[pid] = (x + keep, y, w - keep, h)
            return {"pane": {"pane_id": pid}}
        if (group, verb) == ("pane", "layout"):
            return {"layout": {"area": {"width": 1000, "height": 1000},
                               "panes": [{"pane_id": p, "rect": {"x": r[0], "y": r[1], "width": r[2], "height": r[3]}}
                                         for p, r in self.rects.items()]}}
        if (group, verb) in (("pane", "rename"), ("pane", "run"), ("pane", "send-text")):
            return {"type": "ok"}
        raise AssertionError(f"unexpected herdr call {args}")

    def new_pane(self):
        pid = f"w1:p{self.next_pane}"
        self.next_pane += 1
        return pid


class Build(unittest.TestCase):
    def setUp(self):
        layout.time.sleep = lambda s: None

    def test_grid_ratios_and_cwds(self):
        fake = FakeHerdr()
        lay = layout.parse(MYAPP)
        ids = layout.build(layout.Herdr(fake, "sbx-x"), lay, "/home/dev/code/myapp")
        self.assertEqual(len(ids), 8)
        # The tab's root pane is the first pane of the first row, in its cwd.
        create = next(c for c in fake.calls if c[:2] == ["tab", "create"])
        self.assertIn("/home/dev/code/myapp", create)
        self.assertIn("myapp", create)
        splits = [c for c in fake.calls if c[:2] == ["pane", "split"]]
        self.assertEqual(len(splits), 7)                  # 8 panes from 1 root
        # The first split makes the bottom row, with the cwd of its first pane.
        self.assertEqual(splits[0][3:], ["--direction", "down", "--ratio", "0.5000",
                                          "--cwd", "/home/dev/code/myapp", "--no-focus"])
        # The auth split: 0.25 of what remains after rails took 0.5 = 0.5.
        auth = next(c for c in splits if "/home/dev/code/myapp/services/auth" in c)
        self.assertEqual(auth[6], "0.5000")
        # A sibling cwd is normalised beside the clone; the ui-lib cell opens
        # to the right of `go`, and ui-vue splits down from it, half each.
        ui_lib = next(c for c in splits if "/home/dev/code/ui-lib/packages/core" in c)
        self.assertEqual(ui_lib[4], "right")
        # (that pane is split twice: right for caddy's cell first, then down.)
        vue = next(c for c in splits if c[2] == ids["ui-lib"] and c[4] == "down")
        self.assertEqual(vue[5:7], ["--ratio", "0.5000"])
        self.assertEqual(vue[8], "/home/dev/code/myapp")           # no table: the clone itself
        # Every pane is named, and only the panes with a command are run.
        renames = {c[3] for c in fake.calls if c[:2] == ["pane", "rename"]}
        self.assertEqual(renames, set(lay.names))
        runs = {c[3] for c in fake.calls if c[:2] == ["pane", "run"]}
        self.assertEqual(runs, {"bin/rails s", "docker compose up", "yarn build:watch"})
        # And the geometry that the fake's layout reports is the grid asked for.
        self.assertEqual(layout.check(layout.Herdr(fake, "sbx-x"), lay, ids), [])

    def test_no_run_types_instead(self):
        fake = FakeHerdr()
        layout.build(layout.Herdr(fake, "sbx-x"), layout.parse(MYAPP), "/home/dev/code/myapp", run=False)
        self.assertFalse([c for c in fake.calls if c[:2] == ["pane", "run"]])
        self.assertEqual(len([c for c in fake.calls if c[:2] == ["pane", "send-text"]]), 3)

    def test_existing_tab_needs_replace(self):
        fake = FakeHerdr(tabs=[{"tab_id": "w1:t3", "label": "myapp"}])
        lay = layout.parse(MYAPP)
        with self.assertRaises(layout.LayoutError):
            layout.build(layout.Herdr(fake, "sbx-x"), lay, "/home/dev/code/myapp")
        self.assertFalse([c for c in fake.calls if c[:2] == ["tab", "create"]])
        layout.build(layout.Herdr(fake, "sbx-x"), lay, "/home/dev/code/myapp", replace=True)
        self.assertIn(["tab", "close", "w1:t3"], fake.calls)

    def test_check_reports_a_wrong_rectangle(self):
        fake = FakeHerdr()
        lay = layout.parse(MYAPP)
        ids = layout.build(layout.Herdr(fake, "sbx-x"), lay, "/home/dev/code/myapp")
        fake.rects[ids["caddy"]] = (0, 0, 10, 10)
        problems = layout.check(layout.Herdr(fake, "sbx-x"), lay, ids)
        self.assertEqual(len(problems), 1)
        self.assertIn("caddy", problems[0])


if __name__ == "__main__":
    unittest.main()
