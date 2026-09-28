"""Template definitions (sbxlib/templates.py) and `sbx template`: what a
definition may say, what the build reads from it, when a template is out of
date, and the order of the host steps. Each test works on a copy of the
repository's template files, so nothing here writes into the checkout."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cli, templates
from sbxlib.run import Runner
from tests.test_cli import FakeApi

REPO = Path(__file__).resolve().parent.parent


class Repo(unittest.TestCase):
    """A copy of templates/ and template/ with no local files, as the root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        shutil.copytree(REPO / "template", self.root / "template",
                        ignore=shutil.ignore_patterns("local", "__pycache__"))
        (self.root / "templates").mkdir()
        for p in (REPO / "templates").glob("*.toml"):
            shutil.copy(p, self.root / "templates" / p.name)
        self.local = self.root / "templates" / "local"
        self._root_patch = mock.patch.object(templates, "REPO_ROOT", self.root)
        self._root_patch.start()

    def tearDown(self):
        self._root_patch.stop()
        self._tmp.cleanup()

    def define(self, name: str, text: str, local: bool = True) -> None:
        d = self.local if local else self.root / "templates"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.toml").write_text(text)


class DefinitionTest(Repo):
    def test_every_shipped_preset_loads(self):
        defs = templates.load_all()
        self.assertTrue({"minimal", "rails", "go", "rust", "python"} <= set(defs))
        for name, d in defs.items():
            with self.subTest(name=name):
                self.assertTrue(d.description)
                self.assertEqual(len(templates.fingerprint(d)), 12)

    def test_a_bare_definition_skips_the_core(self):
        d = templates.load("sidecar")
        self.assertTrue(d.bare)
        env = templates.build_env(d)
        self.assertEqual((env["SBX_TEMPLATE_BARE"], env["SBX_COMPONENTS"], env["SBX_TEMPLATE_CORES"]), ("1", "sidecar", "1"))
        self.assertEqual(templates.build_env(templates.load("rails"))["SBX_TEMPLATE_BARE"], "0")
        self.define("odd", 'description = "x"\ncomponents = []\nbare = "yes"\n')
        with self.assertRaisesRegex(templates.TemplateError, "bare must be true or false"):
            templates.load("odd")

    def test_settings_become_build_variables(self):
        env = templates.build_env(templates.load("rails"))
        self.assertEqual(env["SBX_COMPONENTS"], "ruby rails")
        self.assertEqual(env["SBX_RUBY_VERSIONS"], "3.4.10")
        self.assertEqual(env["SBX_DOCKER_IMAGES"], "postgres:17 redis:7")
        self.assertEqual(env["SBX_TEMPLATE_DISK_GB"], "60")
        self.assertEqual(env["SBX_TEMPLATE_NAME"], "rails")

    def test_a_local_definition_wins_over_a_shared_one(self):
        self.define("rails", 'description = "mine"\ncomponents = ["ruby"]\n')
        d = templates.load("rails")
        self.assertEqual((d.description, d.local), ("mine", True))

    def test_a_local_component_wins_and_is_listed(self):
        comp = self.root / "template" / "components" / "local"
        comp.mkdir(parents=True)
        (comp / "zig.sh").write_text("# Zig\nstep zig\nCHECK_TOOLS+=\" zig\"\n")
        self.define("zig", 'components = ["zig"]\n')
        self.assertEqual(templates.load("zig").components, ("zig",))
        self.assertEqual(templates.components()["zig"].parent.name, "local")
        self.assertEqual(templates.describe(templates.components()["zig"]), "Zig")

    def test_mistakes_are_refused(self):
        cases = {
            'components = ["cobol"]': "no component 'cobol'",
            'components = ["rails", "ruby"]': "needs ruby before it",
            'components = ["ruby", "ruby"]': "listed twice",
            'components = []\n[ruby]\nversions = ["3.4.1"]': "not a listed component",
            'components = ["ruby"]\n[ruby]\nVersions = ["3.4.1"]': "lowercase",
            'apt = ["rm -rf /"]': "apt must be",
            'disk_gb = 5': "disk_gb must be",
            'colour = "red"': "unknown key 'colour'",
            'components = ["ruby"]\n[ruby]\nversions = [1, 2]': "a value must be",
        }
        for text, needle in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(templates.TemplateError, needle):
                    templates.parse(text, "x", self.local / "x.toml")
        with self.assertRaisesRegex(templates.TemplateError, "not a template name"):
            templates.parse("", "Bad_Name", self.local / "Bad_Name.toml")

    def test_derived_versions_come_after_the_definitions_own(self):
        templates.write_derived("rails", {"ruby": {"versions": ["3.3.6", "3.4.10"]},
                                          "node": {"versions": ["22"]}})
        templates.write_derived("go", {"node": {"versions": ["20"]}})  # a second section stays
        env = templates.build_env(templates.load("rails"))
        # The definition's first version stays first: it is the template's default.
        self.assertEqual(env["SBX_RUBY_VERSIONS"], "3.4.10 3.3.6")
        self.assertEqual(env["SBX_NODE_VERSIONS"], "lts/* 22")
        self.assertEqual(templates.derived("go"), {"node": {"versions": ["20"]}})

    def test_derived_ruby_is_ignored_for_a_template_without_ruby(self):
        templates.write_derived("go", {"ruby": {"versions": ["3.3.6"]}})
        self.assertNotIn("SBX_RUBY_VERSIONS", templates.build_env(templates.load("go")))

    def test_the_fingerprint_follows_what_goes_into_the_template(self):
        d = templates.load("rails")
        before = templates.fingerprint(d)
        # A file that `sbx new` refreshes from the checkout needs no rebuild.
        (self.root / "template" / "files" / "zshenv").write_text("# changed\n")
        self.assertEqual(templates.fingerprint(d), before)
        # A component that the template uses does.
        ruby = self.root / "template" / "components" / "ruby.sh"
        ruby.write_text(ruby.read_text() + "\n# changed\n")
        self.assertNotEqual(templates.fingerprint(d), before)
        # A component that it does not use does not.
        after = templates.fingerprint(d)
        rust = self.root / "template" / "components" / "rust.sh"
        rust.write_text(rust.read_text() + "\n# changed\n")
        self.assertEqual(templates.fingerprint(d), after)

    def test_the_host_entry_point_prints_what_bash_reads(self):
        # The host runs the module as a script: no sbxlib import may be needed.
        (self.root / "sbxlib").mkdir()
        shutil.copy(REPO / "sbxlib" / "templates.py", self.root / "sbxlib" / "templates.py")
        self.define("mine", 'description = "it\'s mine"\ncomponents = ["ruby"]\n[ruby]\nversions = ["3.4.1", "3.3.6"]\n')
        script = f'eval "$(python3 {self.root}/sbxlib/templates.py env mine)"; ' \
                 'echo "$SBX_RUBY_VERSIONS|$SBX_COMPONENTS|${#SBX_TEMPLATE_HASH}"'
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "3.4.1 3.3.6|ruby|12", done.stderr)
        done = subprocess.run(["python3", f"{self.root}/sbxlib/templates.py", "env", "nope"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("no template definition 'nope'", done.stderr)


class BuildConfTest(unittest.TestCase):
    """host/lib.sh write_build_conf: what the build VM sources. The first real
    build failed because a multi-line variable of another name (the parser's
    whole output) had lines that started with SBX_, and a filter over `set`
    wrote them into the file, where they overwrote the real values."""

    def test_only_the_sbx_variables_arrive_and_each_arrives_whole(self):
        from tests.test_rails_example import BASH
        if BASH is None:
            self.skipTest("needs bash 4 or later")
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "build.conf"
            script = f"""
                set -euo pipefail
                source {REPO}/host/lib.sh
                envtext="$(python3 {REPO}/sbxlib/templates.py env minimal)"
                eval "$envtext"
                SBX_QUOTED="it's \\"quoted\\" \\$HOME"
                write_build_conf {conf}
            """
            done = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            text = conf.read_text()
            self.assertNotIn("envtext", text)
            check = f"set -eu; source {conf}; " \
                    'printf "%s|%s|%s|%s" "$SBX_COMPONENTS" "$SBX_NODE_VERSIONS" "$SBX_TEMPLATE_NAME" "$SBX_QUOTED"'
            done = subprocess.run(["env", "-i", BASH, "-c", check], capture_output=True, text=True)
        self.assertEqual(done.stdout, '|lts/*|minimal|it\'s "quoted" $HOME', done.stderr)


class HostHelpersTest(unittest.TestCase):
    """The version helpers of host/30-template-build.sh, under the script's
    own `set -euo pipefail`, with a stub pvesh and qm. The second real build
    stopped after "is ready": a failed && chain on the last VM of the list (a
    template from before named templates) became the loop's status."""

    VMS = [
        {"type": "qemu", "vmid": 9002, "template": 1, "name": "sbx-tpl-minimal-20260925-0045",
         "tags": "sbx-template;sbx-tpl-minimal;sbx-h-new"},
        {"type": "qemu", "vmid": 9003, "template": 1, "name": "sbx-tpl-minimal-20260101-0900",
         "tags": "sbx-template;sbx-tpl-minimal;sbx-h-old"},
        {"type": "qemu", "vmid": 9004, "template": 1, "name": "sbx-tpl-minimal-20250101-0900",
         "tags": "sbx-template;sbx-tpl-minimal;sbx-h-older"},
        {"type": "qemu", "vmid": 9000, "template": 1, "name": "sbx-base-20260924", "tags": "sbx-template"},
    ]

    def run_prune(self, vms, clones=()):
        import json
        from tests.test_rails_example import BASH
        if BASH is None:
            self.skipTest("needs bash 4 or later")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            text = (REPO / "host" / "30-template-build.sh").read_text()
            (tmp / "fns.sh").write_text(text[text.index("valid_name()"):text.index("# --- guest helpers")])
            (tmp / "bin").mkdir()
            (tmp / "vms.json").write_text(json.dumps(vms))
            (tmp / "bin" / "pvesh").write_text(f"#!/bin/bash\ncat {tmp}/vms.json\n")
            (tmp / "bin" / "qm").write_text(f'#!/bin/bash\necho "qm $*" >> {tmp}/calls\n')
            for f in (tmp / "bin").iterdir():
                f.chmod(0o755)
            conf = tmp / "nodes" / "pve" / "qemu-server"
            conf.mkdir(parents=True)
            for clone, base in clones:  # a linked clone names its base volume
                (conf / f"{clone}.conf").write_text(f"scsi0: local-lvm:base-{base}-disk-1/vm-{clone}-disk-1,size=60G\n")
            script = (f"set -euo pipefail; log() {{ echo \"==> $*\"; }}; warn() {{ echo \"W $*\"; }}; "
                      f"source {tmp}/fns.sh; prune minimal 1; echo DONE")
            env = {**os.environ, "PATH": f"{tmp}/bin:{os.environ['PATH']}", "SBX_PVE_NODES_DIR": str(tmp / "nodes")}
            done = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)
            calls = (tmp / "calls").read_text() if (tmp / "calls").exists() else ""
        return done, calls

    def test_one_version_and_an_old_template_last_is_not_an_error(self):
        done, calls = self.run_prune([self.VMS[0], self.VMS[3]])
        self.assertIn("DONE", done.stdout, done.stderr)
        self.assertEqual(calls, "")

    def test_old_versions_go_unless_a_sandbox_uses_them(self):
        done, calls = self.run_prune(self.VMS, clones=[(9105, 9003)])
        self.assertIn("DONE", done.stdout, done.stderr)
        self.assertEqual(calls, "qm destroy 9004 --purge 1\n")  # 9003 has a clone; 9002 is the newest
        self.assertIn("keeping sbx-tpl-minimal-20260101-0900 (9003): 1 sandbox(es) use it", done.stdout)


class BundleTest(Repo):
    """`sbx template export` and `import`: one file carries a definition and
    its own components to another person's setup, and back unchanged."""

    ZIG = "# Zig\n# a tricky line: ''' and \\n and \"quotes\"\nstep zig\nCHECK_TOOLS+=\" zig\"\n"

    def setUp(self):
        super().setUp()
        comp = self.root / "template" / "components" / "local"
        comp.mkdir(parents=True)
        (comp / "zig.sh").write_text(self.ZIG)
        self.define("tools", 'description = "Zig and Go"\ncomponents = ["go", "zig"]\n[go]\nversion = "1.27.1"\n')
        # A second setup: the same shared files, nothing local.
        self.other = Path(self._tmp.name) / "other"
        shutil.copytree(self.root / "template", self.other / "template",
                        ignore=shutil.ignore_patterns("local"))
        shutil.copytree(self.root / "templates", self.other / "templates",
                        ignore=shutil.ignore_patterns("local"))

    def test_a_round_trip_carries_everything_exactly(self):
        bundle = templates.read_bundle(templates.export_bundle("tools"))
        self.assertEqual(bundle.definition, (self.local / "tools.toml").read_text())
        self.assertEqual(bundle.components, {"zig": self.ZIG})       # its own component, whole
        self.assertEqual(set(bundle.shared), {"go"})                 # a shared one, by hash only

    def test_an_import_builds_the_same_template_on_the_other_setup(self):
        bundle = templates.read_bundle(templates.export_bundle("tools"))
        plan = templates.plan_import(bundle, root=self.other)
        self.assertEqual((plan.problems, plan.warnings), ([], []))
        templates.apply_import(plan)
        theirs = templates.load("tools", self.other)
        self.assertEqual(theirs.components, ("go", "zig"))
        self.assertEqual(templates.fingerprint(theirs, self.other), templates.fingerprint(templates.load("tools")))

    def test_an_import_of_what_is_here_already_writes_nothing(self):
        plan = templates.plan_import(templates.read_bundle(templates.export_bundle("tools")))
        self.assertEqual((plan.writes, plan.problems), ({}, []))
        self.assertEqual(plan.same, ["definition tools", "component zig"])

    def test_a_name_that_exists_needs_as_or_force(self):
        bundle = templates.read_bundle(templates.export_bundle("tools"))
        (self.other / "templates" / "local").mkdir()
        (self.other / "templates" / "local" / "tools.toml").write_text('description = "mine"\n')
        plan = templates.plan_import(bundle, root=self.other)
        self.assertTrue(any("pass --as" in p for p in plan.problems))
        with self.assertRaises(templates.TemplateError):
            templates.apply_import(plan)
        self.assertEqual(templates.plan_import(bundle, as_name="tools2", root=self.other).problems, [])
        forced = templates.plan_import(bundle, force=True, root=self.other)
        self.assertEqual(forced.problems, [])
        self.assertTrue(any("replaces your definition tools" in w for w in forced.warnings))

    def test_a_different_component_of_the_same_name_needs_force(self):
        comp = self.other / "template" / "components" / "local"
        comp.mkdir()
        (comp / "zig.sh").write_text("# my zig\nCHECK_TOOLS+=\" zig\"\n")
        (self.other / "templates" / "local").mkdir()
        (self.other / "templates" / "local" / "mine.toml").write_text('components = ["zig"]\n')
        bundle = templates.read_bundle(templates.export_bundle("tools"))
        problems = templates.plan_import(bundle, root=self.other).problems
        self.assertTrue(any("a different component zig" in p for p in problems))
        forced = templates.plan_import(bundle, force=True, root=self.other)
        self.assertTrue(any("replaces your component zig, which mine also use" in w for w in forced.warnings))

    def test_shared_components_must_exist_and_are_compared(self):
        bundle = templates.read_bundle(templates.export_bundle("tools"))
        go = self.other / "template" / "components" / "go.sh"
        go.write_text(go.read_text() + "# a newer copy\n")
        plan = templates.plan_import(bundle, root=self.other)
        self.assertEqual(plan.problems, [])
        self.assertTrue(any("shared component go differs" in w for w in plan.warnings))
        go.unlink()
        plan = templates.plan_import(bundle, root=self.other)
        self.assertTrue(any("does not have; pull the latest sbx" in p for p in plan.problems))

    def test_a_broken_file_is_refused(self):
        good = templates.export_bundle("tools")
        cases = {
            "": "no \\[sbx_template\\] table",
            "not toml [": "not valid TOML",
            good.replace("format = 1", "format = 2"): "format 2 is not known",
            good.replace('name = "tools"', 'name = "../x"'): "no valid template",
            good.replace("[sbx_template.components.zig]", '[sbx_template.components."Bad"]'): "not a component name",
            "#" * 1_000_001: "larger than 1 MB",
        }
        for text, needle in cases.items():
            with self.subTest(needle=needle), self.assertRaisesRegex(templates.TemplateError, needle):
                templates.read_bundle(text)
        # A definition that names a component that no one has cannot be imported.
        bad = good.replace('components = ["go", "zig"]', 'components = ["go", "zig", "cobol"]')
        problems = templates.plan_import(templates.read_bundle(bad), as_name="x", root=self.other).problems
        self.assertTrue(any("cobol" in p for p in problems))


class CommandTest(Repo):
    def setUp(self):
        super().setUp()
        home = Path(self._tmp.name) / "cfg"
        home.mkdir()
        (home / "config.toml").write_text('pve_api = "https://192.0.2.10:8006"\n')
        self.patches = [mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(home)}),
                        mock.patch("sbxlib.cli.projects_mod.load", return_value={})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def run_cmd(self, *argv, built=()):
        events = []
        api = FakeApi(events)
        api.resources = list(built)
        runner = Runner(responder=lambda a, d: events.append(("cmd", " ".join(a))) or "")
        return cli.main(["template", *argv], runner=runner, api=api), events

    @staticmethod
    def built(name, vmid, fingerprint, stamp="20260925-1200"):
        return {"type": "qemu", "vmid": vmid, "name": f"sbx-tpl-{name}-{stamp}", "node": "pve", "template": 1,
                "tags": f"sbx-template;sbx-tpl-{name};sbx-h-{fingerprint}"}

    def test_rebuild_copies_everything_then_builds_each_template(self):
        code, events = self.run_cmd("rebuild", "rails", "go", "-y", "--no-versions")
        self.assertEqual(code, 0)
        cmds = [e[1] for e in events if e[0] == "cmd"]
        scp = next(i for i, c in enumerate(cmds) if c.startswith("scp "))
        for part in ("/host", "/gw", "/template", "/templates", "/sbxlib", "root@192.0.2.10:/root/sbx/"):
            self.assertIn(part, cmds[scp])
        builds = [c for c in cmds if "30-template-build.sh" in c]
        self.assertEqual([b.rsplit(" ", 1)[1] for b in builds], ["rails", "go"])
        self.assertTrue(all("-t root@192.0.2.10 SBX_YES=1 bash" in b for b in builds))
        self.assertLess(scp, cmds.index(builds[0]))

    def test_changed_builds_only_what_is_not_current(self):
        current = templates.fingerprint(templates.load("go"))
        code, events = self.run_cmd("rebuild", "--changed", "-y", "--no-versions",
                                    built=[self.built("go", 9002, current), self.built("rails", 9003, "old")])
        self.assertEqual(code, 0)
        names = [e[1].rsplit(" ", 1)[1] for e in events if e[0] == "cmd" and "30-template-build.sh" in e[1]]
        self.assertNotIn("go", names)
        self.assertIn("rails", names)
        self.assertIn("minimal", names)  # not built at all

    def test_rebuild_of_an_unknown_template_builds_nothing(self):
        code, events = self.run_cmd("rebuild", "cobol", "-y")
        self.assertEqual(code, 1)
        self.assertFalse([e for e in events if e[0] == "cmd"])

    def test_the_host_steps_pass_their_arguments(self):
        for argv, want in ((["finish", "rails"], "--finish rails"), (["prune"], "--prune"),
                           (["rm", "rails", "-y"], "--rm rails"), (["adopt", "9000", "default"], "--adopt 9000 default")):
            with self.subTest(argv=argv):
                code, events = self.run_cmd(*argv)
                self.assertEqual(code, 0)
                self.assertTrue(any(e[1].endswith(f"30-template-build.sh {want}") for e in events if e[0] == "cmd"))

    def test_export_then_import_by_file_and_by_url(self):
        import contextlib
        import io
        self.define("tools", 'description = "Go"\ncomponents = ["go"]\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_cmd("export", "tools")[0], 0)
        exported = out.getvalue()
        path = Path(self._tmp.name) / "tools.sbx-template.toml"
        path.write_text(exported)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_cmd("import", str(path), "--as", "tools2", "-y")[0], 0)
        self.assertEqual((self.local / "tools2.toml").read_text(), (self.local / "tools.toml").read_text())

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Resp(exported.encode())) as urlopen, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_cmd("import", "https://example.com/t.toml", "--as", "tools3", "-y")[0], 0)
        self.assertEqual(urlopen.call_args[0][0], "https://example.com/t.toml")
        self.assertTrue((self.local / "tools3.toml").exists())
        self.assertEqual(self.run_cmd("import", "http://example.com/t.toml", "-y")[0], 1)  # https only

    def test_new_writes_a_local_definition(self):
        code, _ = self.run_cmd("new", "web", "--from", "rails")
        self.assertEqual(code, 0)
        self.assertEqual((self.local / "web.toml").read_text(), (self.root / "templates" / "rails.toml").read_text())
        self.assertEqual(self.run_cmd("new", "web")[0], 1)  # exists already
        code, _ = self.run_cmd("new", "blank")
        self.assertEqual(code, 0)
        self.assertEqual(templates.load("blank").components, ())

    def test_list_shows_the_state_of_each_template(self):
        current = templates.fingerprint(templates.load("go"))
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, _ = self.run_cmd("list", built=[self.built("go", 9002, current), self.built("rails", 9003, "old"),
                                                  self.built("rails", 9004, "old", "20260101-0000")])
        self.assertEqual(code, 0)
        rows = {line.split()[0]: line for line in out.getvalue().splitlines()[1:] if line and not line.startswith("*")}
        self.assertIn("current", rows["go"])
        self.assertIn("OUT OF DATE", rows["rails"])
        self.assertIn("9003 (+1 old)", rows["rails"])  # the newest version by its date
        self.assertIn("not built", rows["rust"])


if __name__ == "__main__":
    unittest.main()
