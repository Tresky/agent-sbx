import importlib.util
import sys
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "sbx_mirror", Path(__file__).resolve().parent.parent / "template/files/sbx_mirror.py")
mirror = importlib.util.module_from_spec(_spec)
sys.modules["sbx_mirror"] = mirror  # dataclasses looks the module up by name
_spec.loader.exec_module(mirror)

# Real lines from /proc/net/tcp and /proc/net/tcp6 on Linux.
TCP4 = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1130 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1 0
   1: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1 0
   2: 3500007F:0035 00000000:0000 0A 00000000:00000000 00:00000000 00000000   101        0 3 1 0
   3: 3900500A:1130 0100500A:D431 01 00000000:00000000 00:00000000 00000000  1000        0 4 1 0
"""
TCP6 = """  sl  local_address                         remote_address                        st
   0: 00000000000000000000000001000000:1435 00000000000000000000000000000000:0000 0A
   1: 00000000000000000000000000000000:1F90 00000000000000000000000000000000:0000 0A
"""


class ProcNetTest(unittest.TestCase):
    def test_parse_listeners_only(self):
        self.assertEqual(mirror.parse_proc_net(TCP4),
                         [("127.0.0.1", 4400), ("0.0.0.0", 22), ("127.0.0.53", 53)])
        self.assertEqual(mirror.parse_proc_net(TCP6), [("::1", 5173), ("::", 8080)])


class SelectTest(unittest.TestCase):
    def select(self, listeners, mirrored=(), **kw):
        s = mirror.Settings(**kw)
        return mirror.select_ports(listeners, s, "10.77.0.57", set(mirrored))

    def test_loopback_only_ports_are_chosen_with_their_dial_address(self):
        got = self.select(mirror.parse_proc_net(TCP4) + mirror.parse_proc_net(TCP6))
        self.assertEqual(got, {4400: "127.0.0.1", 5173: "::1"})

    def test_wildcard_listener_is_left_alone(self):
        self.assertEqual(self.select([("0.0.0.0", 5432)]), {})
        self.assertEqual(self.select([("127.0.0.1", 3000), ("0.0.0.0", 3000)]), {})

    def test_own_listener_does_not_cause_a_withdrawal(self):
        listeners = [("127.0.0.1", 4400), ("10.77.0.57", 4400)]
        # Controlled pair: the same table flaps without the `mirrored` exclusion.
        self.assertEqual(self.select(listeners, mirrored=[4400]), {4400: "127.0.0.1"})
        self.assertEqual(self.select(listeners, mirrored=[]), {})

    def test_skip_list_and_range(self):
        listeners = [("127.0.0.1", p) for p in (80, 2019, 9222, 9229, 3000, 45000)]
        self.assertEqual(self.select(listeners), {3000: "127.0.0.1"})

    def test_forced_port_beats_range(self):
        self.assertEqual(self.select([("127.0.0.1", 45000)], force_http={45000}), {45000: "127.0.0.1"})

    def test_dual_stack_loopback_prefers_v4(self):
        self.assertEqual(self.select([("127.0.0.1", 3000), ("::1", 3000)]), {3000: "127.0.0.1"})


class CaddyConfigTest(unittest.TestCase):
    def test_empty_config_has_no_apps(self):
        self.assertEqual(mirror.caddy_config("10.77.0.57", {}), {"admin": {"listen": "127.0.0.1:2019"}})

    def test_one_server_per_port_on_the_routed_address(self):
        conf = mirror.caddy_config("10.77.0.57", {4400: "127.0.0.1", 5173: "::1"}, tls_dir="/t")
        servers = conf["apps"]["http"]["servers"]
        self.assertEqual(servers["p4400"]["listen"], ["10.77.0.57:4400"])
        self.assertEqual(servers["p5173"]["routes"][0]["handle"][0]["upstreams"], [{"dial": "[::1]:5173"}])
        # http_redirect must precede tls or plain http:// on the port breaks.
        self.assertEqual([w["wrapper"] for w in servers["p4400"]["listener_wrappers"]], ["http_redirect", "tls"])
        self.assertEqual(conf["apps"]["tls"]["certificates"]["load_files"][0]["key"], "/t/key.pem")

    def test_a_route_precedes_the_port_upstream_and_stays_on_its_port(self):
        vite = mirror.Route(4400, "/vite-dev/*", "https://127.0.0.1:3036")
        plain = mirror.Route(4400, "/api/*", "http://127.0.0.1:8080")
        elsewhere = mirror.Route(9000, "/x/*", "http://127.0.0.1:1")
        conf = mirror.caddy_config("10.77.0.57", {4400: "127.0.0.1", 5173: "::1"}, routes=[vite, plain, elsewhere])
        routes = conf["apps"]["http"]["servers"]["p4400"]["routes"]
        self.assertEqual([r.get("match") for r in routes],
                         [[{"path": ["/vite-dev/*"]}], [{"path": ["/api/*"]}], None])   # catch-all last
        self.assertEqual(routes[0]["handle"][0]["upstreams"], [{"dial": "127.0.0.1:3036"}])
        self.assertEqual(routes[0]["handle"][0]["transport"], {"protocol": "http", "tls": {"insecure_skip_verify": True}})
        self.assertNotIn("transport", routes[1]["handle"][0])           # an http upstream: plain
        self.assertEqual(len(conf["apps"]["http"]["servers"]["p5173"]["routes"]), 1)  # 9000 is not mirrored


class SettingsTest(unittest.TestCase):
    def test_dropins_add_routes_and_ports(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            main = Path(d) / "mirror.toml"
            main.write_text('http = [4400]\nskip = [2019]\n')
            dropins = Path(d) / "mirror.d"
            dropins.mkdir()
            (dropins / "myapp.toml").write_text(
                'tcp = [5432]\n'
                '[[route]]\nport = 4400\npath = "/vite-dev/*"\nto = "https://127.0.0.1:3036/"\n'
                '[[route]]\nport = "x"\npath = "/bad"\nto = "http://127.0.0.1:1"\n'      # ignored, not fatal
                '[[route]]\nport = 4400\npath = "nope"\nto = "http://127.0.0.1:1"\n')    # ignored: no leading /
            (dropins / "notes.txt").write_text("not a toml file")
            s = mirror.load_settings(str(main), str(dropins))
        self.assertEqual(s.force_http, {4400})
        self.assertEqual(s.force_tcp, {5432})
        self.assertEqual(s.skip, {2019})
        self.assertEqual(s.routes, [mirror.Route(4400, "/vite-dev/*", "https://127.0.0.1:3036")])

    def test_no_files_gives_the_defaults(self):
        s = mirror.load_settings("/nonexistent/mirror.toml", "/nonexistent/mirror.d")
        self.assertEqual((s.skip, s.routes), ({2019, 9222, 9229}, []))


if __name__ == "__main__":
    unittest.main()
