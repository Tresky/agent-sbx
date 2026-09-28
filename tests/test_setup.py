"""`sbx setup` and `sbx doctor`: the values proposed for a new host, the
checks of typed values, the files written, and the order of the host steps."""
import ipaddress
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import doctor, hostsetup
from sbxlib.config import Config, load, parse_env_file
from sbxlib.run import Result, Runner

NETSTAT = """Routing tables

Internet:
Destination        Gateway            Flags               Netif Expire
default            192.168.1.1        UGScg                 en0
10/24              link#14            UCS                   en0      !
10.77/24           link#23            UCS                 utun5
100.64/10          link#23            UCS                 utun5
127                127.0.0.1          UCS                   lo0
169.254            link#14            UCS                   en0      !
192.168.1          link#14            UCS                   en0      !
224.0.0/4          link#14            UmCS                  en0      !
"""

DISC = {
    "node": "pve", "version": "pve-manager/9.2.11",
    "bridges": [{"name": "vmbr0", "cidr": "192.168.1.5/24", "ports": "eno1", "gateway": "192.168.1.1"},
                {"name": "vmbr77", "cidr": "", "ports": "", "gateway": ""}],
    "storage": [{"name": "local", "type": "dir", "content": "iso,vztmpl,backup", "active": True},
                {"name": "local-zfs", "type": "zfspool", "content": "images,rootdir", "active": True},
                {"name": "old-lvm", "type": "lvm", "content": "images,rootdir", "active": True}],
    "guests": [{"vmid": 100, "name": "nas", "type": "qemu"}, {"vmid": 9150, "name": "x", "type": "qemu"}],
    "routes": [{"dst": "default", "dev": "vmbr0", "gateway": "192.168.1.1"},
               {"dst": "192.168.1.0/24", "dev": "vmbr0"}, {"dst": "10.79.0.0/16", "dev": "wg0"}],
    "addresses": [{"dev": "vmbr0", "cidr": "192.168.1.5/24"}],
    "ca": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
    "fingerprint": "AA:BB",
    "sans": ["DNS:pve", "IP Address:192.168.1.5"],
}


class ProposeTest(unittest.TestCase):
    def test_mac_networks_reads_ip_route_too(self):
        # Off macOS, LOCAL_ROUTES is `ip -4 route`: full addresses, and "default".
        nets = hostsetup.mac_networks("default via 192.168.1.1 dev eth0 proto dhcp\n"
                                      "172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1\n"
                                      "192.168.1.0/24 dev eth0 proto kernel scope link src 192.168.1.20\n")
        self.assertEqual([str(n) for n in nets], ["172.17.0.0/16", "192.168.1.0/24"])

    def test_mac_networks_expands_the_short_forms(self):
        nets = hostsetup.mac_networks(NETSTAT)
        self.assertIn(ipaddress.ip_network("10.0.0.0/24"), nets)
        self.assertIn(ipaddress.ip_network("10.77.0.0/24"), nets)
        self.assertIn(ipaddress.ip_network("192.168.1.0/24"), nets)
        for skipped in ("127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4"):
            self.assertNotIn(ipaddress.ip_network(skipped), nets)

    def test_a_new_host_gets_free_values(self):
        got = {c.key: c.value for c in hostsetup.propose(DISC, hostsetup.mac_networks(NETSTAT), {})}
        self.assertEqual(got["SBX_LAN_BRIDGE"], "vmbr0")
        # vmbr77 exists, so the pair moves on.
        self.assertEqual((got["SBX_AGENT_BRIDGE"], got["SBX_PERSONAL_BRIDGE"]), ("vmbr79", "vmbr80"))
        # 10.77/24 is on the Mac and 10.79/16 is on the host, so the pairs 77/78 and 79/80 are out.
        self.assertEqual((got["SBX_AGENT_NET"], got["SBX_PERSONAL_NET"]), ("10.81.0", "10.82.0"))
        # 9150 is taken, so the block moves to 10000.
        self.assertEqual((got["SBX_TEMPLATE_VMID_MIN"], got["SBX_TEMPLATE_VMID_MAX"], got["SBX_GW_CTID"],
                          got["SBX_VMID_MIN"], got["SBX_VMID_MAX"]), ("10000", "10099", "10001", "10100", "10199"))
        # zfs over plain LVM, which cannot make a linked clone.
        self.assertEqual((got["SBX_VM_STORAGE"], got["SBX_GW_STORAGE"]), ("local-zfs", "local-zfs"))
        self.assertEqual((got["SBX_GW_TEMPLATE_STORAGE"], got["SBX_SNIPPET_STORAGE"]), ("local", "local"))

    def test_values_in_local_conf_stay(self):
        current = {"SBX_AGENT_NET": "10.77.0", "SBX_TEMPLATE_VMID_MIN": "9000"}
        got = {c.key: c.value for c in hostsetup.propose(DISC, hostsetup.mac_networks(NETSTAT), current)}
        self.assertEqual((got["SBX_AGENT_NET"], got["SBX_TEMPLATE_VMID_MIN"]), ("10.77.0", "9000"))

    def test_no_bridge_or_no_clone_storage_stops(self):
        with self.assertRaisesRegex(hostsetup.SetupError, "no Linux bridge"):
            hostsetup.propose({**DISC, "bridges": []}, [], {})
        lvm_only = [s for s in DISC["storage"] if s["type"] != "zfspool"]
        with self.assertRaisesRegex(hostsetup.SetupError, "linked clones"):
            hostsetup.propose({**DISC, "storage": lvm_only}, [], {})

    def test_typed_values_are_checked(self):
        mac = hostsetup.mac_networks(NETSTAT)
        good = {"SBX_DOMAIN": "lab.internal", "SBX_AGENT_NET": "10.81.0", "SBX_PERSONAL_NET": "10.82.0",
                "SBX_TEMPLATE_VMID_MIN": "10000", "SBX_GW_CTID": "10001"}
        self.assertEqual(hostsetup.check_choices(good, DISC, mac, {}), [])
        bad = {**good, "SBX_DOMAIN": "lab.local", "SBX_AGENT_NET": "192.168.1", "SBX_GW_CTID": "100"}
        problems = " | ".join(hostsetup.check_choices(bad, DISC, mac, {}))
        for needle in (".local", "SBX_AGENT_NET 192.168.1.0/24 collides", "SBX_GW_CTID 100 is taken"):
            self.assertIn(needle, problems)
        # A value that host/local.conf had is this setup's own: it collides with its own route.
        self.assertEqual(hostsetup.check_choices({**good, "SBX_AGENT_NET": "10.77.0"}, DISC, mac,
                                                 {"SBX_AGENT_NET": "10.77.0"}), [])

    def test_policy_names_this_setup(self):
        text = hostsetup.render_policy(Config(agent_net="10.81.0", personal_net="10.82.0", tailscale_tag="tag:lab"))
        self.assertIn('"10.81.0.0/24": ["tag:lab"]', text)
        self.assertNotIn("10.77.0.0/24", text)

    def test_set_toml_keys_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('# a comment\npve_api = "old"\n# pve_ca_file = "~/x.crt"\ncores = 4\n')
            hostsetup.set_toml_keys(path, {"pve_api": "https://h:8006", "pve_ca_file": "~/y.crt",
                                           "pve_token_command": ["a", "b"]})
            text = path.read_text()
        self.assertEqual(text, '# a comment\npve_api = "https://h:8006"\npve_ca_file = "~/y.crt"\ncores = 4\n'
                               'pve_token_command = ["a", "b"]\n')


class _WizardBase(unittest.TestCase):
    """The whole wizard against a fake host."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.home, self.local = tmp / "cfg", tmp / "local.conf"
        self.home.mkdir()
        self.patches = [mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(self.home),
                                                     "SBX_LOCAL_CONF": str(self.local), "HOME": str(tmp)}),
                        mock.patch("sbxlib.hostsetup.socket.gethostbyname", return_value="10.81.0.1"),
                        mock.patch("sbxlib.hostsetup.shutil.which", return_value="/usr/bin/x"),
                        mock.patch("sbxlib.cli._mkcert_root", return_value=Path("/ca")),
                        mock.patch("sbxlib.doctor.run", return_value=0)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def respond(self, argv, data):
        self.cmds.append(argv)
        cmd = argv[-1]
        if argv == hostsetup.LOCAL_ROUTES:
            return NETSTAT
        if argv[0] == "ssh" and cmd == "bash -s":
            return json.dumps(DISC)
        if argv[0] == "ssh" and "40-api-token.sh --rotate --emit" in cmd:
            return "SBX_TOKEN=sbx@pve!cli=00000000-0000-0000-0000-000000000000\n"
        if argv[0] == "ssh" and any(k in cmd for k in ("ip link show", "tailscale status", "pct status", "qm config")):
            return Result(1)
        if argv[:2] == ["security", "find-generic-password"]:
            return Result(44)
        return ""


class WizardTest(_WizardBase):
    def test_a_fresh_host_runs_every_step_in_order(self):
        self.cmds = []
        answers = iter(["192.168.1.5",   # host
                        "",              # accept the values
                        "",              # the LAN has DHCP
                        "",              # the policy pause
                        "rust minimal",  # the templates to build
                        "",              # build the sidecar template
                        "",              # the Include line
                        ])
        with mock.patch("builtins.input", lambda *_: next(answers)), mock.patch("builtins.print"):
            code = hostsetup.cmd_setup(mock.Mock(host=None, mac_only=False), load(), Runner(responder=self.respond))
        self.assertEqual(code, 0)
        steps = [c[-1] for c in self.cmds if c[0] == "ssh" and "/root/sbx/host/" in c[-1]]
        self.assertEqual([s.split("/host/")[1] for s in steps],
                         ["10-bridges.sh", "20-gw-create.sh", "20-gw-create.sh --tailscale",
                          "30-template-build.sh rust", "30-template-build.sh minimal", "30-template-build.sh sidecar",
                          f"40-api-token.sh --rotate --emit --token-id {hostsetup.token_id()}"])
        # The copy goes before the first host script, and after local.conf exists.
        first_scp = next(i for i, c in enumerate(self.cmds) if c[0] == "scp")
        first_step = next(i for i, c in enumerate(self.cmds) if c[0] == "ssh" and "/root/sbx/host/" in c[-1])
        self.assertLess(first_scp, first_step)
        written = parse_env_file(self.local.read_text())
        self.assertEqual((written["SBX_AGENT_NET"], written["SBX_DOMAIN"]), ("10.81.0", "sbx.internal"))
        store = next(c for c in self.cmds if c[:2] == ["security", "add-generic-password"])
        self.assertEqual(store[-1], "sbx@pve!cli=00000000-0000-0000-0000-000000000000")
        cfg = load()
        self.assertEqual(cfg.pve_api, "https://192.168.1.5:8006")
        self.assertEqual(cfg.pve_token_command, ["security", "find-generic-password", "-s", "sbx-pve-token", "-w"])
        # The address is in the certificate, so the CA is used, not the fingerprint.
        self.assertTrue(cfg.pve_ca_file.endswith("pve-root-ca.crt"))
        self.assertEqual(cfg.pve_fingerprint, "")
        # The first template built becomes the default; the copy carries the definitions.
        self.assertEqual(cfg.default_template, "rust")
        scp = next(c for c in self.cmds if c[0] == "scp")
        self.assertTrue(any(a.endswith("/templates") for a in scp) and any(a.endswith("/sbxlib") for a in scp))

    def test_a_done_host_skips_every_host_step(self):
        self.cmds = []
        self.local.write_text("SBX_AGENT_NET=10.81.0\nSBX_PERSONAL_NET=10.82.0\n")
        done = self.respond
        disc = {**DISC, "guests": DISC["guests"] + [
            {"vmid": 9000, "name": "sbx-tpl-rails-20260925-1200", "type": "qemu", "template": True,
             "tags": ["sbx-template", "sbx-tpl-rails", "sbx-h-abc"]},
            {"vmid": 9005, "name": "sbx-tpl-sidecar-20260925-1200", "type": "qemu", "template": True,
             "tags": ["sbx-template", "sbx-tpl-sidecar", "sbx-h-sc1"]}]}

        def respond(argv, data):
            if argv[0] == "ssh" and argv[-1] == "bash -s":
                return json.dumps(disc)
            if argv[0] == "ssh" and any(k in argv[-1] for k in ("ip link show", "tailscale status", "pct status", "qm config")):
                self.cmds.append(argv)
                return ""
            if argv[:2] == ["security", "find-generic-password"]:
                return ""
            return done(argv, data)

        answers = iter(["192.168.1.5", "", "", "", ""])  # host, values, DHCP, keep the token, Include
        with mock.patch("builtins.input", lambda *_: next(answers)), mock.patch("builtins.print"):
            code = hostsetup.cmd_setup(mock.Mock(host=None, mac_only=False), load(), Runner(responder=respond))
        self.assertEqual(code, 0)
        steps = [c[-1].split("/host/")[1] for c in self.cmds if c[0] == "ssh" and "/root/sbx/host/" in c[-1]]
        self.assertEqual(steps, ["40-api-token.sh --acl-only"])


class ExistingInstallTest(_WizardBase):
    def test_an_existing_install_keeps_its_values(self):
        # The gateway exists under the default id and name, with the default
        # bridge vmbr77: the wizard must not move to vmbr79 or new ids.
        self.cmds = []
        disc = {**DISC, "guests": DISC["guests"] + [
            {"vmid": 9001, "name": "sbx-gw", "type": "lxc"},
            {"vmid": 9000, "name": "sbx-base-20260924", "type": "qemu", "template": True, "tags": ["sbx-template"]}]}
        respond = self.respond

        def responder(argv, data):
            if argv[0] == "ssh" and argv[-1] == "bash -s":
                return json.dumps(disc)
            return respond(argv, data)

        answers = iter(["192.168.1.5", "", "", "", "", "", ""])  # host, values, policy, adopt, sidecar, Include, spare
        with mock.patch("builtins.input", lambda *_: next(answers)), mock.patch("builtins.print"), \
                mock.patch("sbxlib.hostsetup.socket.gethostbyname", return_value="10.77.0.1"):
            hostsetup.cmd_setup(mock.Mock(host=None, mac_only=False), load(), Runner(responder=responder))
        written = parse_env_file(self.local.read_text())
        self.assertEqual((written["SBX_AGENT_BRIDGE"], written["SBX_AGENT_NET"], written["SBX_TEMPLATE_VMID_MIN"]),
                         ("vmbr77", "10.77.0", "9000"))
        steps = [c[-1].split("/host/")[1] for c in self.cmds if c[0] == "ssh" and "/root/sbx/host/" in c[-1]]
        self.assertIn("30-template-build.sh --adopt 9000 default", steps)
        # The adopted template is kept; the only build is the sidecar template, which the setup lacks.
        self.assertEqual([s for s in steps if s.startswith("30-template-build.sh") and "--adopt" not in s],
                         ["30-template-build.sh sidecar"])
        self.assertEqual(load().default_template, "default")


class MacOnlyTest(_WizardBase):
    HOST_CONF = "SBX_DOMAIN=lab.internal\nSBX_AGENT_NET=10.81.0\nSBX_GW_CTID=9001\n"

    def run_mac_only(self, answers):
        self.cmds = []
        disc = {**DISC, "guests": DISC["guests"] + [{"vmid": 9001, "name": "sbx-gw", "type": "lxc"}]}

        def responder(argv, data):
            if argv[0] == "ssh" and argv[-1] == "bash -s":
                self.cmds.append(argv)
                return json.dumps(disc)
            if argv[0] == "ssh" and argv[-1] == "cat /root/sbx/host/local.conf":
                return self.HOST_CONF
            return self.respond(argv, data)

        answers = iter(answers)
        with mock.patch("builtins.input", lambda *_: next(answers)), mock.patch("builtins.print"):
            return hostsetup.cmd_setup(mock.Mock(host="192.168.1.5", mac_only=True), load(),
                                       Runner(responder=responder))

    def test_a_second_mac_takes_the_values_from_the_host_and_its_own_token(self):
        with mock.patch("sbxlib.hostsetup.socket.gethostname", return_value="Coworkers-MacBook.local"):
            code = self.run_mac_only(["", ""])  # the host, the Include line
        self.assertEqual(code, 0)
        self.assertEqual(self.local.read_text(), self.HOST_CONF)
        self.assertFalse(any(c[0] == "scp" for c in self.cmds), "--mac-only must not copy to the host")
        steps = [c[-1].split("/host/")[1] for c in self.cmds if c[0] == "ssh" and "/root/sbx/host/" in c[-1]]
        self.assertEqual(steps, ["40-api-token.sh --rotate --emit --token-id cli-coworkers-macbook"])
        self.assertEqual(load().domain, "lab.internal")

    def test_the_hosts_local_templates_come_along_but_never_over_mine(self):
        import base64
        import io
        import tarfile
        repo = self.local.parent / "repo"
        (repo / "templates" / "local").mkdir(parents=True)
        (repo / "templates" / "local" / "mine.toml").write_text("# my own copy\n")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, body in (("templates/local/web.toml", b"components = []\n"),
                               ("templates/local/mine.toml", b"# the host's copy\n"),
                               ("../escape.toml", b"no")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        payload = base64.b64encode(buf.getvalue()).decode()
        respond = self.respond

        def responder(argv, data):
            if argv[0] == "ssh" and "tar -cf -" in argv[-1]:
                return payload
            return respond(argv, data)

        self.cmds = []
        wizard = hostsetup.Wizard(mock.Mock(host="192.168.1.5", mac_only=True), Runner(responder=responder))
        wizard.target = "root@192.168.1.5"
        with mock.patch("sbxlib.hostsetup.REPO_ROOT", repo), mock.patch("builtins.print"):
            wizard._fetch_local_templates()
        self.assertEqual((repo / "templates" / "local" / "web.toml").read_text(), "components = []\n")
        self.assertEqual((repo / "templates" / "local" / "mine.toml").read_text(), "# my own copy\n")
        self.assertFalse((repo.parent / "escape.toml").exists())

    def test_a_different_local_conf_is_replaced_only_on_a_yes(self):
        self.local.write_text("SBX_DOMAIN=other.internal\n")
        with self.assertRaisesRegex(Exception, "must agree"):
            self.run_mac_only(["", "n"])
        self.assertEqual(self.local.read_text(), "SBX_DOMAIN=other.internal\n")


class DoctorTest(unittest.TestCase):
    def test_token_id_is_safe_for_proxmox(self):
        self.assertEqual(hostsetup.token_id("Alex's MacBook Pro.local"), "cli-alex-s-macbook-pro")
        self.assertEqual(hostsetup.token_id("..."), "cli-mac")

    def test_token_scope(self):
        cfg = Config()
        for path in ("/pool/sbx", "/vms/9000", "/vms/9150", "/storage/local-lvm", "/sdn/zones/localnetwork/vmbr77"):
            self.assertTrue(doctor.token_path_allowed(cfg, path), path)
        for path in ("/", "/vms", "/vms/100", "/storage/local", "/sdn/zones/localnetwork/vmbr0", "/nodes/pve"):
            self.assertFalse(doctor.token_path_allowed(cfg, path), path)

    def test_report_fails_on_one_fail(self):
        with mock.patch("builtins.print"):
            self.assertEqual(doctor.report([doctor.Check("ok", "a"), doctor.Check("WARN", "b")]), 0)
            self.assertEqual(doctor.report([doctor.Check("ok", "a"), doctor.Check("FAIL", "b")]), 1)


if __name__ == "__main__":
    unittest.main()
