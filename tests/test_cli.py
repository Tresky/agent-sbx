"""`sbx new` end to end against a fake process runner: which commands run, in
which order, and what never appears on a command line."""
import datetime as dt
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbxlib import cli, names
from sbxlib.config import ConfigError, load, parse_env_file
from sbxlib.pve import PveError
from sbxlib.run import Result, Runner

SECRET = "s3cret-master-key-value"
MANIFEST = """
[[input]]
name = "key"
kind = "file"
dest = "config/master.key"
secret = true

[[input]]
name = "ui"
kind = "repo"
url = "git@github.com:me/ui.git"
dest = "../ui"
"""


UPID = "UPID:pve:0001:0002:0003:task:9101:sbx@pve!cli:"


SIDECAR_TPL = {"type": "qemu", "vmid": 9005, "name": "sbx-tpl-sidecar-20260925-1200", "node": "pve", "template": 1,
               "tags": "sbx-template;sbx-tpl-sidecar;sbx-h-sc1"}


class FakeApi:
    """Stands in for HttpApi. 9100 is held by a guest OUTSIDE the token's pool:
    it is absent from resources, and only /cluster/nextid knows it is taken.
    With sidecars=True, each existing sandbox has its sidecar at vmid + 50."""

    def __init__(self, events, existing=(), sidecars=False):
        self.events = events
        self.resources = [{"type": "qemu", "vmid": 9000, "name": "sbx-base", "node": "pve", "template": 1,
                           "tags": "sbx-template"}, dict(SIDECAR_TPL)]
        self.resources += [{"type": "qemu", "vmid": 9101 + i, "name": n, "node": "pve", "status": "running",
                            "tags": "sbx;sbx-agent"} for i, n in enumerate(existing)]
        if sidecars:
            self.resources += [{"type": "qemu", "vmid": 9151 + i, "name": f"{n}-sc", "node": "pve", "status": "running",
                                "tags": f"sbx-sidecar;sbx-of-{n};sbx-tpl-sidecar"} for i, n in enumerate(existing)]

    def __call__(self, method, path, params=None):
        params = dict(params or {})
        self.events.append(("api", method, path, params))
        if path == "/cluster/resources":
            return self.resources
        if path == "/cluster/nextid":
            if int(params["vmid"]) == 9100:
                raise PveError("GET /cluster/nextid: 400 Parameter verification failed: VM 9100 already exists")
            return str(params["vmid"])
        if "/tasks/" in path:
            return {"status": "stopped", "exitstatus": "OK"}
        if path.endswith("/agent/network-get-interfaces"):
            return {"result": [{"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
                               {"name": "eth0", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.77.0.57"}]}]}
        if method in ("POST", "DELETE"):
            return UPID
        return None


class NewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self.tmp = Path(self._tmp.name)
        home = tmp / "cfg"
        home.mkdir()
        (home / "config.toml").write_text("")
        (home / "id_ed25519").write_text("PRIV")
        (home / "id_ed25519.pub").write_text("ssh-ed25519 AAAA sbx\n")
        (tmp / "caroot").mkdir()
        (tmp / "caroot/rootCA.pem").write_text("CA")

        self.app = tmp / "app"
        (self.app / ".sandbox").mkdir(parents=True)
        (self.app / "config").mkdir()
        (self.app / ".sandbox/sandbox.toml").write_text(MANIFEST)
        (self.app / "config/master.key").write_text(SECRET)
        git = ["git", "-C", str(self.app)]
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.app)], check=True)
        subprocess.run(git + ["remote", "add", "origin", "git@github.com:me/app.git"], check=True)

        self.agent_keys = 0  # ssh-add -l exit code: 0 = keys present
        self.more_resources = []  # more built templates, for the template choice
        self.patches = [mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": str(home)}),
                        mock.patch("sbxlib.cli.resolves", return_value=True),
                        mock.patch("sbxlib.cli.dns_has", return_value=True),
                        mock.patch("sbxlib.herdr.available", return_value=True),
                        mock.patch("sbxlib.cli.shutil.which", return_value="/usr/bin/mkcert")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def run_new(self, *argv, existing=()):
        """Returns (exit code, events, stdin payloads). `events` is ONE ordered
        log of API calls and local commands, so order can be asserted across both."""
        events, payloads = [], []

        def responder(args, data):
            payloads.append(data)
            events.append(("cmd", " ".join(args)))
            if args[0] == "git" and args[1] == "-C":  # real git for the local checkout
                done = subprocess.run(args, capture_output=True)
                return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
            if args[0] == "ssh" and "-T" in args:  # the GitHub probe with the agent alone
                return Result(1, "", "Hi me! You've successfully authenticated, but GitHub does not provide shell access.")
            if args[0] == "ssh-add":
                return Result(self.agent_keys)
            if args[0] == "mkcert" and "-CAROOT" in args:
                return str(self.tmp / "caroot")
            if args[0] == "mkcert":
                Path(args[args.index("-cert-file") + 1]).write_text("CERT")
                Path(args[args.index("-key-file") + 1]).write_text("KEY")
            return ""

        api = FakeApi(events, existing)
        api.resources += self.more_resources
        code = cli.main(["new", *argv], runner=Runner(responder=responder), api=api)
        return code, events, payloads

    @staticmethod
    def pos(events, kind, *fragments):
        for i, event in enumerate(events):
            if event[0] == kind and all(f in " ".join(map(str, event[1:])) for f in fragments):
                return i
        raise AssertionError(f"no {kind} event with {fragments}")

    RUST = {"type": "qemu", "vmid": 9002, "name": "sbx-tpl-rust-20260925-1200", "node": "pve", "template": 1,
            "tags": "sbx-template;sbx-tpl-rust;sbx-h-abc"}
    RUST_OLD = {"type": "qemu", "vmid": 9003, "name": "sbx-tpl-rust-20260101-0900", "node": "pve", "template": 1,
                "tags": "sbx-template;sbx-tpl-rust;sbx-h-old"}

    def clones(self, events):
        return [e[2] for e in events if e[0] == "api" and e[1] == "POST" and e[2].endswith("/clone")]

    def test_two_templates_and_no_choice_make_no_vm(self):
        self.more_resources = [self.RUST]
        code, events, _ = self.run_new("lab")
        self.assertEqual(code, 1)
        self.assertEqual(self.clones(events), [])

    def test_template_option_clones_the_newest_version(self):
        self.more_resources = [self.RUST_OLD, self.RUST]
        code, events, _ = self.run_new("lab", "--template", "rust")
        self.assertEqual(code, 0)
        # The sidecar first, from its own template; then the sandbox.
        self.assertEqual(self.clones(events), ["/nodes/pve/qemu/9005/clone", "/nodes/pve/qemu/9002/clone"])
        config = next(e[3] for e in events if e[0] == "api" and e[1] == "PUT" and e[2] == "/nodes/pve/qemu/9101/config")
        self.assertIn("sbx-tpl-rust", config["tags"])

    def test_a_template_that_is_not_built_makes_no_vm(self):
        code, events, _ = self.run_new("lab", "--template", "go")
        self.assertEqual(code, 1)
        self.assertEqual(self.clones(events), [])

    def test_the_manifest_names_the_template(self):
        self.more_resources = [self.RUST]
        (self.app / ".sandbox/sandbox.toml").write_text('[recipe]\ntemplate = "rust"\n' + MANIFEST)
        code, events, _ = self.run_new("lab", "--project", str(self.app), "--with", "key")
        self.assertEqual(code, 0)
        self.assertEqual(self.clones(events), ["/nodes/pve/qemu/9005/clone", "/nodes/pve/qemu/9002/clone"])

    def test_agent_without_a_decision_makes_no_vm(self):
        code, events, _ = self.run_new("myapp", "--project", str(self.app))
        self.assertEqual(code, 1)
        self.assertFalse([e for e in events if e[0] == "api"], "the host was contacted before the inputs were settled")

    def test_agent_happy_path(self):
        code, events, payloads = self.run_new("myapp", "--project", str(self.app), "--with", "key")
        self.assertEqual(code, 0)
        pos = lambda *a: self.pos(events, *a)  # noqa: E731

        # 9100 is invisible to the token but taken, so the first free id is
        # 9101 for the sandbox; its sidecar takes the next one, 9102.
        sc_clone = pos("api", "POST", "/nodes/pve/qemu/9005/clone")
        sc_config = pos("api", "PUT", "/nodes/pve/qemu/9102/config")
        sc_start = pos("api", "POST", "/nodes/pve/qemu/9102/status/start")
        clone = pos("api", "POST", "/nodes/pve/qemu/9000/clone")
        config = pos("api", "PUT", "/nodes/pve/qemu/9101/config")
        start = pos("api", "POST", "/nodes/pve/qemu/9101/status/start")
        apply_ = pos("cmd", "-p 2222", "dev@10.77.0.57", "sudo sbx-sidecar-apply")
        recipe = pos("cmd", "sbx-recipe-run code/app .sandbox/setup.sh")
        runner_install = pos("cmd", "sudo install -D -m 0755 -o root -g root /dev/stdin /usr/local/bin/sbx-recipe-run")
        self.assertLess(runner_install, recipe, "the current runner goes in before the recipe runs")
        snapshot = pos("api", "POST", "/nodes/pve/qemu/9101/snapshot")
        order = [sc_clone, sc_config, clone, config, sc_start, start, apply_, recipe, snapshot]
        self.assertEqual(sorted(order), order,
                         "sidecar clone < sandbox clone < starts < sidecar applied < recipe < snapshot")

        self.assertEqual(events[sc_clone][3], {"newid": 9102, "name": "sbx-myapp-sc", "pool": "sbx"})
        self.assertEqual(events[clone][3], {"newid": 9101, "name": "sbx-myapp", "pool": "sbx"})
        sc_params = events[sc_config][3]
        # The sidecar: one leg on the sandbox's VLAN with the wire address, one
        # untagged on the agent bridge with DHCP from the gateway.
        # VLAN 3: the second id of the range (9100 is taken), since a VLAN id
        # stops at 4094 and the VM id cannot be the tag.
        self.assertEqual((sc_params["net0"], sc_params["net1"]), ("virtio,bridge=vmbr77,tag=3", "virtio,bridge=vmbr77"))
        self.assertEqual((sc_params["ipconfig0"], sc_params["ipconfig1"]), ("ip=10.79.0.1/30", "ip=dhcp"))
        self.assertEqual(sc_params["tags"], "sbx-sidecar;sbx-of-sbx-myapp;sbx-tpl-sidecar")
        params = events[config][3]
        # The sandbox: its only NIC on its own VLAN, a static wire address, the
        # sidecar as its router, the gateway as its resolver.
        self.assertEqual(params["net0"], "virtio,bridge=vmbr77,tag=3")
        self.assertEqual(params["ipconfig0"], "ip=10.79.0.2/30,gw=10.79.0.1")
        self.assertEqual((params["nameserver"], params["searchdomain"]), ("10.77.0.1", "sbx.internal"))
        self.assertRegex(params["tags"], r"^sbx;sbx-agent;sbx-tpl-default;sbx-exp-\d{8};sbx-proj-app$")
        self.assertEqual(events[snapshot][3], {"snapname": "clean"})
        pos("cmd", "git clone -q -- git@github.com:me/ui.git code/ui")

        # The secret travels on stdin, never in an argv and never in an API call.
        self.assertFalse([e for e in events if SECRET in " ".join(map(str, e))])
        self.assertIn(SECRET.encode(), payloads)
        # .git/info/exclude hides the sent file and the recipe's env file.
        self.assertIn(b"/config/master.key\n/.sandbox.env\n", payloads)
        # The recipe learns the sandbox's public name and profile from its env file.
        self.assertIn(b"SBX_HOSTNAME=sbx-myapp\nSBX_FQDN=sbx-myapp.sbx.internal\nSBX_PROFILE=agent\n", payloads)
        # An agent sandbox never gets the forwarded SSH agent.
        self.assertFalse([e for e in events if "ForwardAgent=yes" in str(e)])

        # The per-sandbox placeholder: in the sidecar's secret file and in the
        # sandbox's git credentials for the proxy, on stdin only. The sandbox
        # is reached by name alone, never by an address.
        env = next(p for p in payloads if p and p.startswith(b"SBX_SIDECAR_LINK="))
        self.assertIn(b"SBX_SANDBOX_HOSTNAME=sbx-myapp\nSBX_SIDECAR_PORTS=open\n", env)
        creds = next(p for p in payloads if p and p.startswith(b"http://sbx:"))
        placeholder = creds.decode().split(":")[2].split("@")[0]
        self.assertEqual(creds, f"http://sbx:{placeholder}@10.79.0.1:8080\n".encode())
        self.assertIn(f"{placeholder}\n".encode(), payloads)
        self.assertFalse([e for e in events if placeholder in " ".join(map(str, e))])
        pos("cmd", "dev@sbx-myapp.sbx.internal", "url.http://10.79.0.1:8080/github/.insteadOf https://github.com/")
        self.assertFalse([e for e in events if e[0] == "cmd" and "dev@10.79.0.2" in e[1]])
        self.assertTrue(all("dev@10.77.0.57" not in e[1] or "-p 2222" in e[1] for e in events if e[0] == "cmd"),
                        "the address on the sidecar network is the sidecar's, on its own port")

    def test_no_sidecar_is_the_old_flow(self):
        Path(os.environ["SBX_CONFIG_DIR"], "config.toml").write_text("agent_sidecar = false\n")
        code, events, _ = self.run_new("plain")
        self.assertEqual(code, 0)
        self.assertEqual(self.clones(events), ["/nodes/pve/qemu/9000/clone"])
        params = events[self.pos(events, "api", "PUT", "/nodes/pve/qemu/9101/config")][3]
        self.assertEqual((params["net0"], params["ipconfig0"]), ("virtio,bridge=vmbr77", "ip=dhcp"))
        self.assertNotIn("nameserver", params)

    def test_a_missing_sidecar_template_makes_no_vm(self):
        events = []
        api = FakeApi(events)
        api.resources = [r for r in api.resources if "sbx-tpl-sidecar" not in r["tags"]]
        code = cli.main(["new", "lab"], runner=Runner(responder=lambda a, d: ""), api=api)
        self.assertEqual(code, 1)
        self.assertEqual(self.clones(events), [])

    def test_proxy_mode_keeps_the_claude_token_in_the_sidecar(self):
        Path(os.environ["SBX_CONFIG_DIR"], "config.toml").write_text('sidecar_claude = "proxy"\n')
        events, writes = [], []

        def responder(args, data):
            events.append(("cmd", " ".join(args)))
            if args[:2] == ["security", "find-generic-password"] and "sbx-claude-token" in args:
                return Result(0, "sk-ant-oat01-real")
            if args[0] == "ssh" and data:
                writes.append((next(a for a in args if a.startswith("dev@")), data))  # (user@host, stdin)
            if args[0] == "mkcert" and "-CAROOT" in args:
                return str(self.tmp / "caroot")
            if args[0] == "mkcert":
                Path(args[args.index("-cert-file") + 1]).write_text("CERT")
                Path(args[args.index("-key-file") + 1]).write_text("KEY")
            return ""
        code = cli.main(["new", "px"], runner=Runner(responder=responder), api=FakeApi(events))
        self.assertEqual(code, 0)
        to_sidecar = [d for h, d in writes if h == "dev@10.77.0.57"]
        to_vm = [d for h, d in writes if h == "dev@sbx-px.sbx.internal"]
        self.assertIn(b"claude=sk-ant-oat01-real\ngithub=\n", to_sidecar)
        self.assertFalse([d for d in to_vm if b"sk-ant-oat01-real" in d], "the real token never enters the sandbox")
        env = next(d for d in to_vm if b"ANTHROPIC_BASE_URL" in d)
        self.assertIn(b"export ANTHROPIC_BASE_URL=http://10.79.0.1:8080\nexport ANTHROPIC_AUTH_TOKEN=", env)
        self.assertFalse([e for e in events if "sk-ant-oat01-real" in " ".join(map(str, e))])

    def test_rm_destroys_the_sidecar_and_gc_removes_an_orphan(self):
        events = []
        api = FakeApi(events, existing=["sbx-old"], sidecars=True)

        def responder(args, data):
            events.append(("cmd", " ".join(args)))
            return ""
        self.assertEqual(cli.main(["rm", "old", "-y"], runner=Runner(responder=responder), api=api), 0)
        deleted = [e[2] for e in events if e[0] == "api" and e[1] == "DELETE"]
        self.assertEqual(deleted, ["/nodes/pve/qemu/9101", "/nodes/pve/qemu/9151"])
        cmds = [e[1] for e in events if e[0] == "cmd" and e[1].startswith("ssh-keygen -R")]
        self.assertTrue(any("sidecar.sbx-old.sbx.internal" in c for c in cmds), "the sidecar's host key goes too")

        events = []
        api = FakeApi(events, existing=["sbx-gone"], sidecars=True)
        api.resources = [r for r in api.resources if r["name"] != "sbx-gone"]  # the sandbox is gone, the sidecar stays
        self.assertEqual(cli.main(["gc", "-y"], runner=Runner(responder=lambda a, d: ""), api=api), 0)
        deleted = [e[2] for e in events if e[0] == "api" and e[1] == "DELETE"]
        self.assertEqual(deleted, ["/nodes/pve/qemu/9151"])

    def test_ssh_sidecar_uses_the_sidecar_port_and_alias(self):
        api = FakeApi([], existing=["sbx-a"], sidecars=True)
        with mock.patch("sbxlib.cli.os.execvp") as execvp:
            cli.main(["ssh", "a", "--sidecar", "--", "uptime"], runner=Runner(responder=lambda a, d: ""), api=api)
        argv = execvp.call_args.args[1]
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertIn("HostKeyAlias=sidecar.sbx-a.sbx.internal", argv)
        self.assertEqual(argv[-3:], ["dev@sbx-a.sbx.internal", "--", "uptime"])
        api = FakeApi([], existing=["sbx-b"])
        code = cli.main(["ssh", "b", "--sidecar"], runner=Runner(responder=lambda a, d: ""), api=api)
        self.assertEqual(code, 1, "a sandbox without a sidecar has nothing to open")

    def test_personal_profile(self):
        code, events, _ = self.run_new("lab", "--profile", "personal", "--project", str(self.app))
        self.assertEqual(code, 0)
        params = events[self.pos(events, "api", "PUT", "/config")][3]
        self.assertEqual((params["net0"], params["tags"]), ("virtio,bridge=vmbr78", "sbx;sbx-personal;sbx-tpl-default;sbx-proj-app"))
        self.assertFalse([e for e in events if e[0] == "api" and e[2].endswith("/snapshot")])
        forwarded = [e[1] for e in events if e[0] == "cmd" and "ForwardAgent=yes" in e[1]]
        self.assertTrue(forwarded and all("git clone" in line for line in forwarded),
                        "the agent is forwarded for git clones only")

    def test_a_late_name_makes_the_cli_ask_for_the_lease_then_use_the_name(self):
        # dnsmasq has the name only AFTER the sandbox was told to ask for its
        # lease. A sandbox without a sidecar registers its own name; with one,
        # the sidecar does, and the sandbox is never reached by address.
        Path(os.environ["SBX_CONFIG_DIR"], "config.toml").write_text("agent_sidecar = false\n")
        seen = []

        def responder_hook(args):
            seen.append(" ".join(args))

        def late_resolves(name):
            return any("systemctl start sbx-dhcp-hostname" in line for line in seen)

        events, payloads = [], []

        def responder(args, data):
            responder_hook(args)
            events.append(("cmd", " ".join(args)))
            if args[0] == "git" and args[1] == "-C":
                done = subprocess.run(args, capture_output=True)
                return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
            if args[0] == "mkcert" and "-CAROOT" in args:
                return str(self.tmp / "caroot")
            if args[0] == "mkcert":
                Path(args[args.index("-cert-file") + 1]).write_text("CERT")
                Path(args[args.index("-key-file") + 1]).write_text("KEY")
            return ""

        with mock.patch("sbxlib.cli.dns_has", side_effect=lambda server, name: late_resolves(name)), \
             mock.patch("sbxlib.cli.time.sleep"), mock.patch("sbxlib.vm.time.sleep"):
            code = cli.main(["new", "late"], runner=Runner(responder=responder), api=FakeApi(events))
        self.assertEqual(code, 0)
        cmds = [e[1] for e in events if e[0] == "cmd" and e[1].startswith("ssh")]
        kick = next(i for i, c in enumerate(cmds) if "systemctl start sbx-dhcp-hostname" in c)
        self.assertIn("dev@10.77.0.57", cmds[kick], "the kick goes through the address")
        later = cmds[kick + 1:]
        self.assertTrue(later and all("dev@sbx-late.sbx.internal" in c for c in later),
                        f"every command after the kick uses the name: {later}")

    def test_the_project_token_beats_the_global_token(self):
        # Controlled pair: the same agent run, with and without a per-project
        # [git] binding. The sandbox must get the project's token, never the
        # global one, when the binding exists.
        home = Path(os.environ["SBX_CONFIG_DIR"])
        (home / "config.toml").write_text('git_token_command = ["print-token", "global"]\n')
        (home / "bindings").mkdir(exist_ok=True)

        def run(with_binding):
            binding = home / "bindings" / "app.toml"
            if with_binding:
                binding.write_text('[git]\ntoken_command = ["print-token", "app-only"]\n')
            elif binding.exists():
                binding.unlink()
            events, payloads = [], []

            def responder(args, data):
                payloads.append(data)
                events.append(("cmd", " ".join(args)))
                if args[0] == "print-token":
                    return f"ghp_{args[1]}\n"
                if args[0] == "git" and args[1] == "-C":
                    done = subprocess.run(args, capture_output=True)
                    return Result(done.returncode, done.stdout.decode(), done.stderr.decode())
                if args[0] == "mkcert" and "-CAROOT" in args:
                    return str(self.tmp / "caroot")
                if args[0] == "mkcert":
                    Path(args[args.index("-cert-file") + 1]).write_text("CERT")
                    Path(args[args.index("-key-file") + 1]).write_text("KEY")
                return ""
            code = cli.main(["new", "tok", "--project", str(self.app), "--with", "key"],
                            runner=Runner(responder=responder), api=FakeApi(events))
            self.assertEqual(code, 0)
            # The token goes to the SIDECAR's tokens file, and to nothing else.
            self.assertFalse([p for p in payloads if p and p.startswith(b"https://x-access-token:")])
            creds = [p for p in payloads if p and p.startswith(b"claude=")]
            self.assertEqual(len(creds), 1)
            return creds[0]

        self.assertEqual(run(with_binding=True), b"claude=\ngithub=ghp_app-only\n")
        self.assertEqual(run(with_binding=False), b"claude=\ngithub=ghp_global\n")

    def test_personal_with_an_empty_ssh_agent_makes_no_vm(self):
        # Controlled pair with test_personal_profile: the same command, and
        # the only difference is whether the agent holds a key.
        self.agent_keys = 1
        code, events, _ = self.run_new("lab", "--profile", "personal", "--project", str(self.app))
        self.assertEqual(code, 1)
        self.assertFalse([e for e in events if e[0] == "api"], "no API call may happen before the access is proved")

    def test_existing_name_is_refused_before_a_clone(self):
        code, events, _ = self.run_new("myapp", existing=["sbx-myapp"])
        self.assertEqual(code, 1)
        self.assertFalse([e for e in events if e[0] == "api" and e[2].endswith("/clone")])

    def test_new_puts_the_sandbox_in_the_herdr_sidebar_unless_told_not_to(self):
        # Controlled pair: the same run with and without --no-herdr.
        code, events, _ = self.run_new("h1")
        self.assertEqual(code, 0)
        self.assertTrue(any(e[0] == "cmd" and e[1] == "herdr machine add sbx-h1 --label sbx-h1" for e in events))
        code, events, _ = self.run_new("h2", "--no-herdr")
        self.assertEqual(code, 0)
        self.assertFalse([e for e in events if e[0] == "cmd" and e[1].startswith("herdr machine add")])

    def test_a_herdr_timeout_does_not_fail_the_sandbox(self):
        from sbxlib.run import CommandError
        events = []

        def responder(args, data):
            events.append(("cmd", " ".join(args)))
            if args[:3] == ["herdr", "machine", "add"]:
                raise CommandError(args, 124, "timeout")
            if args[0] == "git" and args[1] == "-C":
                r = subprocess.run(args, capture_output=True)
                return Result(r.returncode, r.stdout.decode(), r.stderr.decode())
            if args[0] == "mkcert" and "-CAROOT" in args:
                return str(self.tmp / "caroot")
            if args[0] == "mkcert":
                Path(args[args.index("-cert-file") + 1]).write_text("CERT")
                Path(args[args.index("-key-file") + 1]).write_text("KEY")
            return ""
        code = cli.main(["new", "slow"], runner=Runner(responder=responder), api=FakeApi(events))
        self.assertEqual(code, 0, "the sandbox is complete; herdr is a convenience")

    def test_rm_removes_the_saved_herdr_machine(self):
        events = []
        api = FakeApi(events, existing=["sbx-old"])

        def responder(args, data):
            events.append(("cmd", " ".join(args)))
            if args[:3] == ["herdr", "machine", "list"]:
                return '[{"id": "abc123", "label": "sbx-old", "target": "sbx-old"}, {"id": "zzz", "target": "other"}]'
            return ""
        self.assertEqual(cli.main(["rm", "old", "-y"], runner=Runner(responder=responder), api=api), 0)
        self.assertIn(("cmd", "herdr machine remove abc123"), events)
        self.assertNotIn(("cmd", "herdr machine remove zzz"), events)

    def claude_responder(self, events, payloads, token="sk-ant-oat01-tok", has_file=()):
        """`security` holds `token`; a sandbox in `has_file` has the token file."""
        def responder(args, data):
            events.append(("cmd", " ".join(args)))
            payloads.append(data)
            if args[:2] == ["security", "find-generic-password"] and "sbx-claude-token" in args:
                return Result(0 if token else 44, token)
            if args[0] == "ssh" and "ANTHROPIC_BASE_URL" in args[-1]:  # which kind of token file the sandbox has
                return "direct" if any(f"@{h}." in " ".join(args) for h in has_file) else "none"
            if args[0] == "mkcert" and "-CAROOT" in args:
                return str(self.tmp / "caroot")
            if args[0] == "mkcert":
                Path(args[args.index("-cert-file") + 1]).write_text("CERT")
                Path(args[args.index("-key-file") + 1]).write_text("KEY")
            return ""
        return responder

    def test_new_signs_claude_in_unless_told_not_to(self):
        # Controlled pair: the same run with and without --no-claude.
        for flag, want in (((), True), (("--no-claude",), False)):
            events, payloads = [], []
            code = cli.main(["new", "c1", *flag], runner=Runner(responder=self.claude_responder(events, payloads)),
                            api=FakeApi(events))
            self.assertEqual(code, 0)
            self.assertEqual(b"export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-tok\n" in payloads, want)
            self.assertEqual(any("hasCompletedOnboarding" in e[1] for e in events if e[0] == "cmd"), want)
            # The token travels on stdin, never in an argv.
            self.assertFalse([e for e in events if "sk-ant-oat01-tok" in " ".join(map(str, e))])
        self.assertFalse([e for e in events if "sbx-claude-token" in str(e)], "--no-claude must not read the keychain")

    def test_new_without_a_token_still_makes_the_sandbox(self):
        events, payloads = [], []
        code = cli.main(["new", "c2"], runner=Runner(responder=self.claude_responder(events, payloads, token="")),
                        api=FakeApi(events))
        self.assertEqual(code, 0)
        self.assertFalse([p for p in payloads if p and b"CLAUDE_CODE_OAUTH_TOKEN" in p])

    def test_claude_token_writes_only_where_the_file_is_or_the_name_is_given(self):
        events, payloads = [], []
        api = FakeApi(events, existing=["sbx-a", "sbx-b", "sbx-c", "sbx-off"])
        api.resources[-1]["status"] = "stopped"
        runner = Runner(responder=self.claude_responder(events, payloads, token="sk-ant-oat01-new", has_file=["sbx-a"]))
        with mock.patch("sys.stdin", io.StringIO("sk-ant-oat01-new\n")):
            code = cli.main(["claude-token", "--stdin", "c"], runner=runner, api=api)
        self.assertEqual(code, 0)
        writes = [e[1] for e in events if e[0] == "cmd" and "claude.env" in e[1] and "cat >" in e[1]]
        self.assertEqual(len(writes), 2)
        self.assertTrue(any("@sbx-a." in w for w in writes) and any("@sbx-c." in w for w in writes),
                        "sbx-a has the file; sbx-c is named; sbx-b has neither")
        self.assertFalse([e for e in events if e[0] == "cmd" and "@sbx-off." in e[1]], "a stopped VM is not contacted")
        self.assertIn(b"export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-new\n", payloads)
        self.assertTrue(any(e[1].startswith("security add-generic-password") for e in events if e[0] == "cmd"))
        from sbxlib import claudetoken
        self.assertEqual(claudetoken.expires(), dt.date.today() + claudetoken.LIFETIME)

    def test_claude_token_refuses_an_api_key(self):
        events = []
        with mock.patch("sys.stdin", io.StringIO("sk-ant-api03-xyz\n")):
            code = cli.main(["claude-token", "--stdin"], runner=Runner(responder=self.claude_responder(events, [])),
                            api=FakeApi(events))
        self.assertEqual(code, 1)
        self.assertFalse([e for e in events if "add-generic-password" in str(e)])

    def test_claude_token_expiry_warning(self):
        from sbxlib import claudetoken
        self.assertIsNone(claudetoken.expiry_warning())  # no record, no warning
        made = dt.date(2026, 1, 1)
        claudetoken.store(Runner(responder=lambda a, d: ""), "t", today=made)
        end = made + claudetoken.LIFETIME
        self.assertIsNone(claudetoken.expiry_warning(end - dt.timedelta(days=31)))
        self.assertIn("expires on", claudetoken.expiry_warning(end - dt.timedelta(days=30)))
        self.assertIn("expired on", claudetoken.expiry_warning(end))

    def test_rm_and_gc_touch_only_sandboxes(self):
        events = []
        api = FakeApi(events, existing=["sbx-old"])
        api.resources[-1]["tags"] = "sbx;sbx-agent;sbx-exp-20200101"
        api.resources.append({"type": "qemu", "vmid": 500, "name": "sbx-lookalike", "node": "pve", "tags": ""})
        self.assertEqual(cli.main(["gc", "-y"], runner=Runner(responder=lambda a, d: ""), api=api), 0)
        deleted = [e[2] for e in events if e[0] == "api" and e[1] == "DELETE"]
        # A VM with a matching NAME but no `sbx` tag is not ours to destroy.
        self.assertEqual(deleted, ["/nodes/pve/qemu/9101"])


class GuideTest(unittest.TestCase):
    def test_guide_and_bare_sbx_print_the_same_screen(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"SBX_CONFIG_DIR": tmp}):
            outs = []
            for argv in (["guide"], []):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(argv, runner=Runner(responder=lambda a, d: "")), 0)
                outs.append(out.getvalue())
        self.assertEqual(outs[0], outs[1])
        text = outs[0]
        for needle in ("sbx new lab", "sbx git-token", "sbx.internal", "agent", "personal", "usage.md",
                       "9001 sbx-gw", "sbx-tpl-*", "sbx template rebuild", "vmbr77, vmbr78", "10.77.0.0/24"):
            self.assertIn(needle, text)
        self.assertLess(text.count("\n"), 80, "the guide must fit two screens at most")


class SmallTests(unittest.TestCase):
    def test_hostname(self):
        self.assertEqual(names.hostname("myapp"), "sbx-myapp")
        self.assertEqual(names.hostname("sbx-my-game"), "sbx-my-game")
        for bad in ["", "MyApp", "a_b", "-a", "a-", "a--b", "gw", "template", "base-1", "x" * 70, "a.b"]:
            with self.subTest(bad=bad), self.assertRaises(names.NameError_):
                names.hostname(bad)

    def test_project_name(self):
        self.assertEqual(names.project_name("git@github.com:example/ui-kit.git"), "ui-kit")
        self.assertEqual(names.project_name("https://github.com/a/b/"), "b")

    def test_defaults_env_is_read_by_python_as_bash_reads_it(self):
        env = parse_env_file('A=1\n# c\nB="x y"  # tail\nC=\n')
        self.assertEqual(env, {"A": "1", "B": "x y", "C": ""})
        none = Path("/nonexistent")
        cfg = load(config_path=none, local_path=none)
        self.assertEqual((cfg.domain, cfg.agent_bridge, cfg.template_pool), ("sbx.internal", "vmbr77", "sbx-templates"))

    def test_config_defaults_match_defaults_conf(self):
        # A Config() made in code must agree with the file that the host reads.
        from sbxlib.config import _ENV_MAP, Config, DEFAULTS_ENV
        shared = parse_env_file(DEFAULTS_ENV.read_text())
        plain = Config()
        for env_key, field in _ENV_MAP.items():
            with self.subTest(key=env_key):
                self.assertEqual(str(getattr(plain, field)), shared[env_key])

    def test_vlan_follows_the_id_range_and_stays_in_range(self):
        cfg = load()
        self.assertEqual((cfg.vlan_for(9100), cfg.vlan_for(9101), cfg.vlan_for(9199)), (2, 3, 101))
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.conf"
            local.write_text("SBX_VMID_MIN=100\nSBX_VMID_MAX=9999\n")
            with self.assertRaisesRegex(ConfigError, "wider than the 4093 VLANs"):
                load(local_path=local)

    def test_config_toml_cannot_disagree_with_a_shared_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "config.toml"
            conf.write_text('domain = "sbx.internal"\n')
            self.assertEqual(load(config_path=conf).domain, "sbx.internal")
            conf.write_text('domain = "other.internal"\n')
            with self.assertRaisesRegex(ConfigError, "SBX_DOMAIN in host/local.conf"):
                load(config_path=conf)

    def test_local_conf_overrides_the_defaults_for_the_mac_too(self):
        # One override file for the host scripts AND the Mac tool: a bridge
        # renamed for the host must not leave `sbx new` on the old bridge.
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.conf"
            local.write_text("SBX_AGENT_BRIDGE=vmbr90\nSBX_DOMAIN=lab.internal\nSBX_GW_CTID=123\n")
            cfg = load(config_path=Path("/nonexistent"), local_path=local)
        self.assertEqual((cfg.agent_bridge, cfg.personal_bridge, cfg.domain), ("vmbr90", "vmbr78", "lab.internal"))


if __name__ == "__main__":
    unittest.main()
