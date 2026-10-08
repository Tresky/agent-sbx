"""sbx fork: the original is only read; the copy is cleaned while its new
sidecar lets nothing out; the copy gets its own VLAN, tags and credentials."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cli
from sbxlib.pve import Pve, Sandbox
from sbxlib.run import Runner

SRC = Sandbox(9107, "sbx-app", "pve", "running",
              ("sbx", "sbx-agent", "sbx-tpl-rails", "sbx-proj-app", "sbx-exp-20261001", "sbx-autostart"))
SRC_SC = Sandbox(9108, "sbx-app-sc", "pve", "running", ("sbx-sidecar", "sbx-of-sbx-app"))
# Anything the fork may do to the original, by Pve method.
ALLOWED_ON_SOURCE = {"snapshot", "clone_from_snapshot", "delete_snapshot"}


class ForkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "config.toml").write_text("")
        (home / "id_ed25519").write_text("key")
        (home / "id_ed25519.pub").write_text("ssh-ed25519 AAAA sbx\n")
        env = mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(home)})
        env.start()
        self.addCleanup(env.stop)
        self.log = []   # one ordered record of everything: Pve calls and commands in VMs

    def pve(self, net0="virtio=BC:24:11:00:00:01,bridge=vmbr77,tag=9"):
        pve = mock.create_autospec(Pve, instance=True)
        pve.require.return_value = SRC
        pve.sidecars.return_value = {"sbx-app": SRC_SC}
        pve.find.return_value = None
        pve.next_vmid.side_effect = lambda start=None: 9110 if start is None else start
        pve.vm_config.return_value = {"net0": net0, "onboot": 1}
        pve.template.return_value = mock.Mock(name="sidecar-template")
        for name in ("snapshot", "clone_from_snapshot", "delete_snapshot", "configure", "create_sidecar", "start"):
            getattr(pve, name).side_effect = (lambda n: lambda *a, **k: self.log.append(("pve", n, a, k)))(name)
        return pve

    def run_fork(self, pve, *extra):
        sc, vm = mock.Mock(), mock.Mock()
        vm.cfg = cli.load_config()
        sc.run.side_effect = lambda cmd, **k: self.log.append(("sidecar", cmd))
        sc.put.side_effect = lambda data, dest, **k: self.log.append(("sidecar-put", dest, data))
        vm.run.side_effect = lambda cmd, **k: self.log.append(("vm", cmd))
        vm.put.side_effect = lambda data, dest, **k: self.log.append(("vm-put", dest, data))

        def provision(*a, **k):
            self.log.append(("provision", k.get("quarantine"), k.get("refresh"), a[6]))
            return sc
        args = cli.build_parser().parse_args(["fork", "app", "app2", "--no-herdr", *extra])
        with mock.patch("sbxlib.cli._pve", return_value=pve), \
             mock.patch("sbxlib.cli._provision_sidecar", side_effect=provision), \
             mock.patch("sbxlib.cli._connect", return_value=vm), \
             mock.patch("sbxlib.cli._install_cert", return_value=False), \
             mock.patch("sbxlib.cli._git_token", return_value=("github.com", "x-access-token", "ghp_real")), \
             mock.patch("sbxlib.cli._git_auth", side_effect=lambda *a, **k: self.log.append(("git_auth", a[5]))), \
             mock.patch("sbxlib.cli.claudetoken.get", return_value="sk-ant-oat01-real"):
            return cli.cmd_fork(args, cli.load_config(), Runner(responder=lambda a, d: ""))

    def test_the_original_is_only_snapshotted_cloned_from_and_its_snapshot_removed(self):
        pve = self.pve()
        self.assertEqual(self.run_fork(pve), 0)
        for kind, name, a, k in [e for e in self.log if e[0] == "pve"]:
            if SRC.vmid in a[:2] or SRC_SC.vmid in a[:2]:
                self.assertIn(name, ALLOWED_ON_SOURCE, f"{name} touched the original: {a}")
        # Never a stop, a shutdown, a rollback or a destroy, on anything.
        called = {c[0] for c in pve.method_calls}
        self.assertFalse(called & {"stop", "shutdown", "rollback", "destroy", "set_autostart", "set_expiry"})
        snaps = [e for e in self.log if e[0] == "pve" and e[1] in ("snapshot", "delete_snapshot")]
        self.assertEqual([(e[1], e[2][1], e[2][2]) for e in snaps],
                         [("snapshot", 9107, "fork-sbx-app2"), ("delete_snapshot", 9107, "fork-sbx-app2")])

    def test_the_copy_gets_its_own_vlan_tags_and_no_autostart(self):
        pve = self.pve()
        self.run_fork(pve, "--ttl", "0")
        (conf,) = [e for e in self.log if e[0] == "pve" and e[1] == "configure"]
        node, vmid, params = conf[2]
        self.assertEqual(vmid, 9110)
        vlan = cli.load_config().vlan_for(9110)
        self.assertIn(f"tag={vlan}", params["net0"])
        self.assertNotIn("tag=9,", params["net0"] + ",")
        self.assertIn("BC:24:11:00:00:01", params["net0"], "the MAC that Proxmox gave the copy is kept")
        self.assertEqual(params["onboot"], 0)
        tags = params["tags"].split(";")
        self.assertIn("sbx-fork-of-app", tags)
        self.assertIn("sbx-proj-app", tags)
        self.assertNotIn("sbx-autostart", tags)
        self.assertFalse([t for t in tags if t.startswith("sbx-exp-")], "--ttl 0: no expiry")
        (sc,) = [e for e in self.log if e[0] == "pve" and e[1] == "create_sidecar"]
        self.assertEqual(sc[3]["vlan"], vlan)

    def test_the_copy_is_cleaned_before_its_quarantine_is_lifted(self):
        self.run_fork(self.pve())
        kinds = [e[0] if e[0] != "vm" else ("clean" if "credentials.json" in e[1] else "vm") for e in self.log]
        prov = kinds.index("provision")
        self.assertTrue(self.log[prov][1], "the sidecar starts in quarantine")
        self.assertTrue(self.log[prov][2], "with the sidecar files of this checkout")
        clean = kinds.index("clean")
        lift = next(i for i, e in enumerate(self.log) if e[0] == "sidecar-put" and e[1] == "/etc/sbx/sidecar.env")
        self.assertLess(prov, clean)
        self.assertLess(clean, lift)
        self.assertNotIn(b"QUARANTINE", self.log[lift][2])
        self.assertEqual(self.log[lift + 1], ("sidecar", "sudo sbx-sidecar-apply"))
        # The copy's credentials name the NEW secret, and git uses it too.
        secret = self.log[prov][3]
        proxy = [e for e in self.log if e[0] == "vm-put" and e[1].endswith("claude.env")]
        self.assertTrue(proxy and secret.encode() in proxy[0][2])
        self.assertIn(("git_auth", secret), self.log)

    def test_the_clean_script_removes_the_login_and_every_claude(self):
        for needle in ("rm -f ~/.claude/.credentials.json", "sbx-remote-control.service", "sbx-claude-resume.service",
                       "sbx-claude-track.timer", "pkill", "systemd-machine-id-setup",
                       "test ! -e ~/.claude/.credentials.json"):
            self.assertIn(needle, cli.FORK_CLEAN)

    def test_refusals_come_before_anything_is_made(self):
        pve = self.pve()
        pve.sidecars.return_value = {}
        with self.assertRaises(cli.InputError):
            self.run_fork(pve)
        pve = self.pve()
        pve.find.return_value = SRC
        with self.assertRaises(cli.PveError):
            self.run_fork(pve)
        self.assertFalse([e for e in self.log if e[0] == "pve"], "nothing was made")

    def test_a_copy_with_no_vlan_tag_stops_before_it_starts(self):
        pve = self.pve(net0="virtio=BC:24:11:00:00:01,bridge=vmbr77")
        with self.assertRaises(cli.PveError):
            self.run_fork(pve)
        self.assertFalse([e for e in self.log if e[0] == "pve" and e[1] == "start"])



class GitAuthAgainTest(unittest.TestCase):
    """_git_auth runs on a sandbox that has the proxy rewrites already (a fork)."""

    def test_it_writes_the_rewrites_again_with_real_git(self):
        import subprocess
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "GIT_CONFIG_NOSYSTEM": "1"}

            class LocalVm:
                cfg = cli.load_config()

                def run(self, cmd, **k):
                    done = subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, text=True)
                    assert done.returncode == 0, done.stderr

                def put(self, data, dest, **k):
                    Path(home, dest).write_bytes(data)
            project = cli.Project("app", "", None, None, None)
            for _ in range(2):   # the second run is the fork's
                cli._git_auth(cli.load_config(), Runner(), LocalVm(), "agent", project, "placeholder")
            got = subprocess.run(["git", "config", "--global", "--get-all",
                                  "url.http://10.79.0.1:8080/github/.insteadOf"],
                                 env=env, capture_output=True, text=True).stdout.split()
            self.assertEqual(got, ["https://github.com/", "git@github.com:", "ssh://git@github.com/"])

if __name__ == "__main__":
    unittest.main()
