"""The two host-side scripts of the sidecar design, run with stub commands:
sidecar/sbx-sidecar-apply against a directory of its own, and the stanza
functions of host/10-bridges.sh against a sample /etc/network/interfaces.
Neither has a host to run on before the first real setup; this is what can
be proved without one."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_rails_example import BASH as MODERN_BASH

REPO = Path(__file__).resolve().parent.parent
# These two scripts need no bash 4 feature; the system bash of a Mac (3.2)
# runs them, and is the stricter check. A newer bash is used when present.
BASH = MODERN_BASH or (shutil.which("bash") if shutil.which("bash") else None)

IP_ADDR = """2: ens18    inet 10.79.0.1/30 scope global ens18\\       valid_lft forever preferred_lft forever
3: ens19    inet 10.77.0.57/24 metric 100 brd 10.77.0.255 scope global dynamic ens19\\       valid_lft 3421sec preferred_lft 3421sec
"""
IP_ROUTE = "default via 10.77.0.1 dev ens19 proto dhcp src 10.77.0.57 metric 100\n"


def stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        if BASH is None:
            self.skipTest("needs bash 4 or later")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.calls = self.root / "calls"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        log = f'echo "$(basename "$0") $*" >> {self.calls}\n'
        stub(bin_dir, "ip", log + f'''case "$*" in
  "-o -4 addr show") printf '%s' "$IP_ADDR" ;;
  "-o -4 addr show dev ens19") printf '%s' "$IP_ADDR" | grep ens19 ;;
  "-4 route show default") printf '%s' "$IP_ROUTE" ;;
esac
''')
        stub(bin_dir, "nft", log)
        stub(bin_dir, "systemctl", log)
        stub(bin_dir, "hostnamectl", log + f'[[ "$1" == set-hostname ]] && echo "$2" > {self.root}/hostname\n')
        stub(bin_dir, "networkctl", log)
        stub(bin_dir, "hostname", f"cat {self.root}/hostname\n")
        (self.root / "hostname").write_text("sbx-app-sc\n")
        (self.root / "etc/sbx/sidecar").mkdir(parents=True)
        (self.root / "usr/local/lib/sbx").mkdir(parents=True)
        (self.root / "usr/local/lib/sbx/sidecar-nftables.tmpl").write_bytes((REPO / "sidecar/nftables.conf.tmpl").read_bytes())
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SBX_SIDECAR_ROOT": str(self.root),
                    "IP_ADDR": IP_ADDR, "IP_ROUTE": IP_ROUTE}

    def tearDown(self):
        self._tmp.cleanup()

    def write_env(self, ports="open", hostname="sbx-app"):
        (self.root / "etc/sbx/sidecar.env").write_text(
            f"SBX_SIDECAR_LINK=10.79.0\nSBX_AGENT_NET=10.77.0\nSBX_SANDBOX_HOSTNAME={hostname}\n"
            f"SBX_SIDECAR_PORTS={ports}\nSBX_CLAUDE_UPSTREAM=https://api.anthropic.com\n"
            "SBX_GIT_UPSTREAM=https://github.com\n")

    def apply(self, *args, stdin=""):
        done = subprocess.run([BASH, str(REPO / "sidecar/sbx-sidecar-apply"), *args], env=self.env,
                              input=stdin, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def test_renders_the_firewall_for_the_interfaces_it_finds(self):
        self.write_env()
        out = self.apply()
        conf = (self.root / "etc/nftables.conf").read_text()
        self.assertNotIn("@SBX_", conf, "every token was rendered")
        self.assertIn('iifname "ens18" tcp dport { 8080, 8081 } accept', conf)
        self.assertIn('iifname "ens19" ip saddr 10.77.0.0/24 ip saddr != 10.77.0.1 drop', conf)
        self.assertIn('iifname "ens19" tcp dport 22 dnat to 10.79.0.2', conf)
        self.assertIn('iifname "ens19" tcp dport @approved dnat to 10.79.0.2', conf)
        self.assertIn('iifname "ens19" tcp dport 1024-32767 dnat to 10.79.0.2', conf, "open mode: the whole range")
        self.assertIn('oifname "ens19" ip saddr 10.79.0.0/24 masquerade', conf)
        self.assertIn('ip daddr 10.77.0.1 udp dport 53 accept', conf)
        calls = self.calls.read_text().splitlines()
        self.assertIn(f"nft -c -f {self.root}/etc/nftables.conf.new", calls, "checked before it is installed")
        self.assertIn(f"nft -f {self.root}/etc/nftables.conf", calls)
        self.assertIn("systemctl restart sbx-sidecar", calls)
        self.assertEqual((self.root / "run/sbx/sidecar.env").read_text(), "SBX_SIDECAR_NET_ADDR=10.77.0.57\n")
        for name in ("secret", "tokens"):
            mode = stat.S_IMODE((self.root / "etc/sbx/sidecar" / name).stat().st_mode)
            self.assertEqual(mode, 0o600, name)
        self.assertIn("wire ens18 (10.79.0.1), network ens19 (10.77.0.57), ports open", out)

    def test_ask_mode_opens_no_range(self):
        self.write_env(ports="ask")
        self.apply()
        conf = (self.root / "etc/nftables.conf").read_text()
        self.assertNotIn("1024-32767 dnat", conf)
        self.assertIn("# ports open by approval only", conf)
        self.assertIn("tcp dport 22 dnat to 10.79.0.2", conf, "port 22 is always the sandbox's")

    def test_registers_the_sandboxs_name_once(self):
        self.write_env(hostname="sbx-app")
        self.apply()
        calls = self.calls.read_text()
        self.assertIn("hostnamectl set-hostname sbx-app", calls)
        self.assertIn("networkctl reconfigure ens19", calls)
        self.calls.write_text("")
        self.apply()  # the hostname is right now, as after a boot where nothing changed it
        self.assertNotIn("hostnamectl", self.calls.read_text())

    def test_no_env_file_is_a_quiet_no_op(self):
        out = self.apply()
        self.assertIn("nothing to apply", out)
        self.assertFalse((self.root / "etc/nftables.conf").exists())

    def test_claude_token_replaces_one_line_and_keeps_the_other(self):
        self.write_env()
        tokens = self.root / "etc/sbx/sidecar/tokens"
        tokens.write_text("claude=old\ngithub=ghp_keep\n")
        self.apply("--claude-token", stdin="sk-ant-oat01-new\n")
        self.assertEqual(sorted(tokens.read_text().splitlines()), ["claude=sk-ant-oat01-new", "github=ghp_keep"])
        self.assertEqual(stat.S_IMODE(tokens.stat().st_mode), 0o600)
        self.assertIn("systemctl restart sbx-sidecar", self.calls.read_text())
        self.apply("--claude-token", stdin="\n")
        self.assertEqual(sorted(tokens.read_text().splitlines()), ["claude=", "github=ghp_keep"])

    def test_tunnel_token_starts_the_tunnel_and_an_empty_one_stops_it(self):
        self.write_env()
        env = self.root / "etc/sbx/sidecar/cloudflared.env"
        self.apply("--tunnel-token", stdin="eyJhIjoiYWJjIn0=\n")
        self.assertEqual(env.read_text(), "TUNNEL_TOKEN=eyJhIjoiYWJjIn0=\n")
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertIn("systemctl restart sbx-cloudflared", self.calls.read_text())
        self.apply("--tunnel-token", stdin="\n")
        self.assertFalse(env.exists())
        self.assertIn("systemctl disable -q --now sbx-cloudflared", self.calls.read_text())

    def test_a_tunnel_token_with_a_newline_trick_is_refused(self):
        # The token becomes one line of an env file: nothing may add a second.
        self.write_env()
        done = subprocess.run([BASH, str(REPO / "sidecar/sbx-sidecar-apply"), "--tunnel-token"], env=self.env,
                              input="abc ExecStart=x\n", capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse((self.root / "etc/sbx/sidecar/cloudflared.env").exists())

    def test_a_missing_wire_is_an_error(self):
        self.write_env()
        self.env["IP_ADDR"] = IP_ADDR.replace("10.79.0.1/30", "10.79.0.9/30")
        done = subprocess.run([BASH, str(REPO / "sidecar/sbx-sidecar-apply")], env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("no interface holds 10.79.0.1/30", done.stderr)


INTERFACES_OLD = """auto lo
iface lo inet loopback

auto vmbr0
iface vmbr0 inet static
\taddress 192.168.1.5/24
\tgateway 192.168.1.1
\tbridge-ports eno1
\tbridge-stp off
\tbridge-fd 0

auto vmbr77
iface vmbr77 inet manual
\tbridge-ports none
\tbridge-stp off
\tbridge-fd 0
#sbx: agent sandbox subnet. The gateway container 9001 routes it.

auto vmbr78
iface vmbr78 inet manual
\tbridge-ports none
\tbridge-stp off
\tbridge-fd 0
#sbx: personal sandbox subnet. The gateway container 9001 routes it.
"""


class BridgesTest(unittest.TestCase):
    """The stanza functions of host/10-bridges.sh: what a fresh host gets,
    and what a host from before sidecars gets added."""

    def setUp(self):
        if BASH is None:
            self.skipTest("needs bash 4 or later")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        text = (REPO / "host/10-bridges.sh").read_text()
        self.fns = text[text.index("IFACES=/etc/network/interfaces"):text.index("missing=()")]
        self.ifaces = self.tmp / "interfaces"

    def tearDown(self):
        self._tmp.cleanup()

    def run_fns(self, script: str) -> str:
        prelude = (f"set -euo pipefail; SBX_GW_CTID=9001; SBX_AGENT_BRIDGE=vmbr77; SBX_PERSONAL_BRIDGE=vmbr78\n"
                   + self.fns.replace("IFACES=/etc/network/interfaces", f"IFACES={self.ifaces}"))
        done = subprocess.run([BASH, "-c", prelude + script], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_a_fresh_stanza_has_the_vlan_and_ipv6_lines(self):
        agent = self.run_fns("stanza vmbr77 agent")
        self.assertIn("iface vmbr77 inet manual\n\tbridge-ports none\n\tbridge-stp off\n\tbridge-fd 0\n"
                      "\tbridge-vlan-aware yes\n\tbridge-vids 2-4094\n"
                      "\tpost-up sysctl -qw net.ipv6.conf.$IFACE.disable_ipv6=1\n", agent)
        personal = self.run_fns("stanza vmbr78 personal")
        self.assertNotIn("vlan", personal)
        self.assertIn("disable_ipv6=1", personal)

    def test_an_old_stanza_gets_exactly_the_missing_lines(self):
        self.ifaces.write_text(INTERFACES_OLD)
        self.assertEqual(self.run_fns("missing_lines vmbr77 agent").splitlines(),
                         ["bridge-vlan-aware yes", "bridge-vids 2-4094",
                          "post-up sysctl -qw net.ipv6.conf.$IFACE.disable_ipv6=1"])
        self.assertEqual(self.run_fns("missing_lines vmbr78 personal").splitlines(),
                         ["post-up sysctl -qw net.ipv6.conf.$IFACE.disable_ipv6=1"])
        self.run_fns('add_lines vmbr77 "bridge-vlan-aware yes" "bridge-vids 2-4094" "$NO_IPV6"')
        self.run_fns('add_lines vmbr78 "$NO_IPV6"')
        text = self.ifaces.read_text()
        self.assertIn("iface vmbr77 inet manual\n\tbridge-ports none\n\tbridge-stp off\n\tbridge-fd 0\n"
                      "\tbridge-vlan-aware yes\n\tbridge-vids 2-4094\n"
                      "\tpost-up sysctl -qw net.ipv6.conf.$IFACE.disable_ipv6=1\n#sbx: agent", text)
        self.assertIn("iface vmbr78 inet manual\n\tbridge-ports none\n\tbridge-stp off\n\tbridge-fd 0\n"
                      "\tpost-up sysctl -qw net.ipv6.conf.$IFACE.disable_ipv6=1\n#sbx: personal", text)
        # The LAN bridge and everything else is untouched.
        self.assertIn("iface vmbr0 inet static\n\taddress 192.168.1.5/24\n\tgateway 192.168.1.1\n"
                      "\tbridge-ports eno1\n\tbridge-stp off\n\tbridge-fd 0\n\nauto vmbr77", text)
        # Nothing is missing any more.
        self.assertEqual(self.run_fns("missing_lines vmbr77 agent"), "")
        self.assertEqual(self.run_fns("missing_lines vmbr78 personal"), "")


if __name__ == "__main__":
    unittest.main()
