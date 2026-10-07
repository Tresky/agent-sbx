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
                        "n",             # no 1Password: a key file
                        "",              # the Include line
                        ])
        with mock.patch("builtins.input", lambda *_: next(answers)), mock.patch("builtins.print"):
            code = hostsetup.cmd_setup(mock.Mock(host=None, local_only=False), load(), Runner(responder=self.respond))
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

        answers = iter(["192.168.1.5", "", "", "", "n", ""])  # host, values, DHCP, keep the token, no 1Password, Include
        with mock.patch("builtins.input", lambda *_: next(answers)), mock.patch("builtins.print"):
            code = hostsetup.cmd_setup(mock.Mock(host=None, local_only=False), load(), Runner(responder=respond))
        self.assertEqual(code, 0)
        steps = [c[-1].split("/host/")[1] for c in self.cmds if c[0] == "ssh" and "/root/sbx/host/" in c[-1]]
        self.assertEqual(steps, ["40-api-token.sh --acl-only"])

    def test_a_running_gateway_without_tailscale_is_set_up_again(self):
        # An earlier run failed inside gw/setup.sh: the container runs, but
        # Tailscale is not installed, so `tailscale up` cannot work yet.
        self.cmds = []
        self.local.write_text("SBX_AGENT_NET=10.81.0\nSBX_PERSONAL_NET=10.82.0\n")
        respond = self.respond

        def responder(argv, data):
            if argv[0] == "ssh" and "command -v tailscale" in argv[-1]:
                self.cmds.append(argv)
                return Result(1)
            if argv[0] == "ssh" and "pct status" in argv[-1]:
                self.cmds.append(argv)
                return ""
            return respond(argv, data)

        answers = iter(["192.168.1.5", "", "", "", "rust", "", "", ""])
        with mock.patch("builtins.input", lambda *_: next(answers)), mock.patch("builtins.print"):
            hostsetup.cmd_setup(mock.Mock(host=None, local_only=False), load(), Runner(responder=responder))
        steps = [c[-1].split("/host/")[1] for c in self.cmds if c[0] == "ssh" and "/root/sbx/host/" in c[-1]]
        self.assertIn("20-gw-create.sh", steps)
        self.assertLess(steps.index("20-gw-create.sh"), steps.index("20-gw-create.sh --tailscale"))


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

        answers = iter(["192.168.1.5", "", "", "", "", "n", "", ""])  # host, values, policy, adopt, sidecar, no 1Password, Include, spare
        with mock.patch("builtins.input", lambda *_: next(answers)), mock.patch("builtins.print"), \
                mock.patch("sbxlib.hostsetup.socket.gethostbyname", return_value="10.77.0.1"):
            hostsetup.cmd_setup(mock.Mock(host=None, local_only=False), load(), Runner(responder=responder))
        written = parse_env_file(self.local.read_text())
        self.assertEqual((written["SBX_AGENT_BRIDGE"], written["SBX_AGENT_NET"], written["SBX_TEMPLATE_VMID_MIN"]),
                         ("vmbr77", "10.77.0", "9000"))
        steps = [c[-1].split("/host/")[1] for c in self.cmds if c[0] == "ssh" and "/root/sbx/host/" in c[-1]]
        self.assertIn("30-template-build.sh --adopt 9000 default", steps)
        # The adopted template is kept; the only build is the sidecar template, which the setup lacks.
        self.assertEqual([s for s in steps if s.startswith("30-template-build.sh") and "--adopt" not in s],
                         ["30-template-build.sh sidecar"])
        self.assertEqual(load().default_template, "default")


class LocalOnlyTest(_WizardBase):
    HOST_CONF = "SBX_DOMAIN=lab.internal\nSBX_AGENT_NET=10.81.0\nSBX_GW_CTID=9001\n"

    def run_local_only(self, answers):
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
            return hostsetup.cmd_setup(mock.Mock(host="192.168.1.5", local_only=True), load(),
                                       Runner(responder=responder))

    def test_a_second_mac_takes_the_values_from_the_host_and_its_own_token(self):
        with mock.patch("sbxlib.hostsetup.socket.gethostname", return_value="Coworkers-MacBook.local"):
            code = self.run_local_only(["", "n", ""])  # the host, no 1Password, the Include line
        self.assertEqual(code, 0)
        self.assertEqual(self.local.read_text(), self.HOST_CONF)
        self.assertFalse(any(c[0] == "scp" for c in self.cmds), "--local-only must not copy to the host")
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
        wizard = hostsetup.Wizard(mock.Mock(host="192.168.1.5", local_only=True), Runner(responder=responder))
        wizard.target = "root@192.168.1.5"
        with mock.patch("sbxlib.hostsetup.REPO_ROOT", repo), mock.patch("builtins.print"):
            wizard._fetch_local_templates()
        self.assertEqual((repo / "templates" / "local" / "web.toml").read_text(), "components = []\n")
        self.assertEqual((repo / "templates" / "local" / "mine.toml").read_text(), "# my own copy\n")
        self.assertFalse((repo.parent / "escape.toml").exists())

    def test_a_different_local_conf_is_replaced_only_on_a_yes(self):
        self.local.write_text("SBX_DOMAIN=other.internal\n")
        with self.assertRaisesRegex(Exception, "must agree"):
            self.run_local_only(["", "n"])
        self.assertEqual(self.local.read_text(), "SBX_DOMAIN=other.internal\n")


class SandboxKeyTest(_WizardBase):
    """Wizard._sandbox_key: a key file here, or an item in 1Password that its
    SSH agent serves. The agent and `op` are faked; no real key is touched."""

    PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA sbx"
    NEW = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB sbx"
    DUMMY_PRIVATE = "-----BEGIN OPENSSH PRIVATE KEY-----DUMMYDUMMY"

    def setUp(self):
        super().setUp()
        self.sock = self.home.parent / "agent.sock"
        self.sock.write_text("")
        self.agent = []        # the public key lines that the fake agent lists
        self.agent_code = None  # force an exit code of ssh-add
        self.items = []        # SSH Key items in the fake vault
        self.whoami = 0
        self.signin = []       # whoami codes after each op signin; empty: it changes nothing
        self.op_version = "2.30.0"
        self.created = []      # the item create argv
        self.keygen = []
        self.prompts, self.out = [], []

    def respond_key(self, argv, data):
        if argv[:2] == ["env", f"SSH_AUTH_SOCK={self.sock}"] and argv[2:] == ["ssh-add", "-L"]:
            if self.agent_code is not None:
                return Result(self.agent_code)
            return Result(0, "\n".join(self.agent) + "\n") if self.agent else Result(1)
        if argv == ["op", "whoami"]:
            return Result(self.whoami)
        if argv == ["op", "signin"]:
            if self.signin:
                self.whoami = self.signin.pop(0)
            return Result(self.whoami)
        if argv == ["op", "--version"]:
            return self.op_version + "\n"
        if argv[:3] == ["op", "item", "list"]:
            return json.dumps(self.items)
        if argv[:2] == ["op", "read"]:
            return self.NEW if argv[2] == "op://v1/i-new/public key" else self.PUB
        if argv[:3] == ["op", "item", "create"]:
            self.created.append(argv)
            self.agent.append(self.NEW)  # 1Password serves the new item at once
            return json.dumps({"id": "i-new", "vault": {"id": "v1"},
                               "fields": [{"id": "private_key", "value": self.DUMMY_PRIVATE}]})
        if argv[0] == "ssh-keygen":
            self.keygen.append(argv)
            return ""
        raise AssertionError(f"unexpected command {argv}")

    def run_key(self, answers, *, op=True, sandboxes=()):
        """`answers` are strings, or callables that run when a prompt is shown."""
        it = iter(answers)

        def fake_input(prompt=""):
            self.prompts.append(prompt)
            got = next(it)
            if callable(got):
                got()
                return ""
            return got

        wizard = hostsetup.Wizard(mock.Mock(host="h", local_only=True), Runner(responder=self.respond_key))
        say = lambda *a, **k: self.out.append(" ".join(map(str, a)))  # noqa: E731
        fake_pve = mock.Mock(sandboxes=lambda: [mock.Mock(hostname=n) for n in sandboxes])
        with mock.patch("builtins.input", fake_input), mock.patch("builtins.print", say), \
                mock.patch("sbxlib.cli.warn", say), mock.patch("sbxlib.cli.info", say), \
                mock.patch("sbxlib.cli._pve", return_value=fake_pve), \
                mock.patch("sbxlib.onepassword.shutil.which", return_value="/usr/bin/op" if op else None):
            wizard._sandbox_key()
        self.assertEqual(list(it), [], "answers left over: the wizard asked less than expected")
        return wizard

    def files(self, private=False, pub=None):
        if private:
            (self.home / "id_ed25519").write_text("PRIV")
        if pub:
            (self.home / "id_ed25519.pub").write_text(pub + "\n")

    def conf(self):
        path = self.home / "config.toml"
        return path.read_text() if path.exists() else ""

    # --- no 1Password ---

    def test_no_with_no_files_makes_a_key_file(self):
        self.run_key(["n"])
        self.assertEqual(len(self.keygen), 1)
        self.assertEqual(self.keygen[0][-1], str(self.home / "id_ed25519"))
        self.assertNotIn("ssh_agent", self.conf())

    def test_no_keeps_an_existing_key_file(self):
        self.files(private=True, pub=self.PUB)
        self.run_key(["n"])
        self.assertEqual(self.keygen, [])

    def test_no_with_only_the_pub_stops_instead_of_making_a_new_key(self):
        self.files(pub=self.PUB)
        with self.assertRaisesRegex(hostsetup.SetupError, "lock you out"):
            self.run_key(["n"])
        self.assertEqual(self.keygen, [])

    def test_no_clears_a_set_agent(self):
        self.files(private=True, pub=self.PUB)
        (self.home / "config.toml").write_text(f'ssh_agent = "{self.sock}"\ncores = 4\n')
        wizard = self.run_key(["n"])
        self.assertEqual(wizard.cfg.ssh_agent, "")
        self.assertIn("cores = 4", self.conf())
        self.assertIn("1Password", self.prompts[0])
        self.assertIn("[Y/n]", self.prompts[0], "the default is yes when an agent is set")

    # --- 1Password, the key is there already ---

    def test_yes_with_the_key_listed_saves_the_agent_and_offers_removal(self):
        self.files(private=True, pub=self.PUB)
        self.agent = [self.PUB]
        wizard = self.run_key(["y", str(self.sock), "n"], op=False)
        self.assertEqual(wizard.cfg.ssh_agent, str(self.sock))
        self.assertTrue((self.home / "id_ed25519").exists())
        self.assertIn("Remove", self.prompts[-1])
        self.assertEqual(self.keygen, [])

    def test_yes_to_the_removal_deletes_the_private_file(self):
        self.files(private=True, pub=self.PUB)
        self.agent = [self.PUB]
        self.run_key(["y", str(self.sock), "y"], op=False)
        self.assertFalse((self.home / "id_ed25519").exists())
        self.assertTrue((self.home / "id_ed25519.pub").exists())

    def test_with_only_the_pub_and_a_listed_key_nothing_is_asked_after_the_socket(self):
        self.files(pub=self.PUB)
        self.agent = [self.PUB]
        wizard = self.run_key(["y", str(self.sock)], op=False)
        self.assertEqual(wizard.cfg.ssh_agent, str(self.sock))

    def test_an_existing_sbx_item_gives_its_pub_and_creates_nothing(self):
        self.items = [{"id": "i-old", "title": "other", "vault": {"id": "v0"}},
                      {"id": "i-sbx", "title": "sbx", "vault": {"id": "v1"}}]
        self.agent = [self.PUB]
        wizard = self.run_key(["y", str(self.sock)])
        pub = self.home / "id_ed25519.pub"
        self.assertEqual(pub.read_text(), self.PUB + "\n")
        self.assertEqual(pub.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.created, [])
        self.assertEqual(wizard.cfg.ssh_agent, str(self.sock))
        self.assertIn(f'ssh_agent = "{self.sock}"', self.conf())

    # --- 1Password, a new key ---

    def test_no_key_anywhere_makes_one_with_op_and_prints_no_secret(self):
        wizard = self.run_key(["y", str(self.sock), "Work"])
        self.assertEqual(len(self.created), 1)
        argv = self.created[0]
        self.assertEqual(argv[:3], ["op", "item", "create"])
        self.assertIn("ed25519", argv)
        self.assertEqual(argv[argv.index("--category") + 1], "SSH Key")
        self.assertEqual(argv[argv.index("--vault") + 1], "Work")
        self.assertNotIn("--reveal", argv)
        self.assertEqual((self.home / "id_ed25519.pub").read_text(), self.NEW + "\n")
        self.assertFalse((self.home / "id_ed25519").exists())
        self.assertEqual(wizard.cfg.ssh_agent, str(self.sock))
        everything = "\n".join(self.out + self.prompts + [self.conf()])
        self.assertNotIn("DUMMYDUMMY", everything)
        self.assertNotIn("PRIVATE KEY", everything)

    def test_an_existing_file_and_new_makes_a_key_and_names_the_sandboxes(self):
        self.files(private=True, pub=self.PUB)
        wizard = self.run_key(["y", str(self.sock), "n", ""], sandboxes=["lab", "myapp"])
        self.assertEqual(len(self.created), 1)
        self.assertEqual((self.home / "id_ed25519.pub").read_text(), self.NEW + "\n")
        warning = next(o for o in self.out if "OLD key" in o)
        self.assertIn("lab, myapp", warning)
        # The old file is not offered for removal: it is another key.
        self.assertFalse(any("Remove" in p for p in self.prompts))
        self.assertTrue((self.home / "id_ed25519").exists())
        self.assertEqual(wizard.cfg.ssh_agent, str(self.sock))

    def test_an_existing_file_and_import_goes_through_the_app_and_creates_nothing(self):
        self.files(private=True, pub=self.PUB)
        wizard = self.run_key(["y", str(self.sock), "i", lambda: self.agent.append(self.PUB), "n"])
        self.assertEqual(self.created, [])
        self.assertIn(f"Import {self.home / 'id_ed25519'}", next(p for p in self.prompts if "Import" in p))
        self.assertEqual((self.home / "id_ed25519.pub").read_text(), self.PUB + "\n")
        self.assertIn("Remove", self.prompts[-1])
        self.assertEqual(wizard.cfg.ssh_agent, str(self.sock))

    def test_an_imported_key_that_the_agent_never_lists_stops(self):
        self.files(private=True, pub=self.PUB)
        with self.assertRaisesRegex(hostsetup.SetupError, "does not list"):
            self.run_key(["y", str(self.sock), "i", "", ""])
        self.assertNotIn("ssh_agent", self.conf())

    # --- no op ---

    def test_without_op_it_says_how_to_install_it_and_can_stop(self):
        with self.assertRaisesRegex(hostsetup.SetupError, "install"):
            self.run_key(["y", str(self.sock), "n"], op=False)
        text = "\n".join(self.out)
        self.assertIn("1Password CLI (`op`) is not installed", text)
        self.assertIn("developer.1password.com/docs/cli/get-started", text)
        self.assertIn("sbx setup --local-only", text)
        self.assertEqual(self.created, [])

    def test_without_op_the_key_is_picked_from_the_agent_list(self):
        self.agent = [self.NEW.replace(" sbx", " other")]
        wizard = self.run_key(["y", str(self.sock), "y", lambda: self.agent.append(self.PUB), "2"], op=False)
        self.assertEqual((self.home / "id_ed25519.pub").read_text(), self.PUB + "\n")
        self.assertEqual(wizard.cfg.ssh_agent, str(self.sock))
        self.assertTrue(any("2. ssh-ed25519 sbx" in o for o in self.out))

    def test_op_too_old_for_ssh_key_items_stops_and_says_to_update(self):
        self.op_version = "2.6.1"
        with self.assertRaisesRegex(hostsetup.SetupError, r"version 2\.6\.1.*need 2\.20\.0 or later.*op update"):
            self.run_key(["y", str(self.sock)])
        self.assertEqual(self.created, [])

    def test_op_that_is_not_signed_in_signs_in_here(self):
        self.whoami = 1
        self.signin = [0]
        self.items = [{"id": "i1", "title": "sbx", "vault": {"id": "v1"}}]
        self.agent = [self.PUB]
        self.run_key(["y", str(self.sock)])
        self.assertFalse(any("Integrate with 1Password CLI" in p for p in self.prompts))

    def test_a_failed_signin_pauses_once_then_signs_in_again(self):
        self.whoami = 1
        self.signin = [1, 0]
        self.items = [{"id": "i1", "title": "sbx", "vault": {"id": "v1"}}]
        self.agent = [self.PUB]
        self.run_key(["y", str(self.sock), ""])
        self.assertTrue(any("Integrate with 1Password CLI" in p for p in self.prompts))

    def test_op_that_cannot_sign_in_pauses_once_then_stops(self):
        self.whoami = 1
        with self.assertRaisesRegex(hostsetup.SetupError, "sign in"):
            self.run_key(["y", str(self.sock), ""])
        self.assertTrue(any("Integrate with 1Password CLI" in p for p in self.prompts))

    # --- the agent ---

    def test_a_missing_socket_pauses_then_stops(self):
        missing = self.home.parent / "gone.sock"
        with self.assertRaisesRegex(hostsetup.SetupError, "still does not exist"):
            self.run_key(["y", str(missing), ""])
        self.assertIn("Use the SSH agent", self.prompts[-1])

    def test_an_agent_that_cannot_be_reached_stops(self):
        self.agent_code = 2
        with self.assertRaisesRegex(hostsetup.SetupError, "cannot reach the agent"):
            self.run_key(["y", str(self.sock)], op=False)

    def test_the_default_socket_is_the_configured_one(self):
        self.files(private=True, pub=self.PUB)
        self.agent = [self.PUB]
        (self.home / "config.toml").write_text(f'ssh_agent = "{self.sock}"\n')
        self.run_key(["", "", "n"], op=False)
        self.assertIn(str(self.sock), self.prompts[1])


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
