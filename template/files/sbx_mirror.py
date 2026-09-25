#!/usr/bin/env python3
"""sbx-mirror: re-expose loopback-only listeners on the routed address.

A dev server binds 127.0.0.1:4400 by default. This service opens <routed-ip>:4400
and forwards to it, so https://<vm-name>:4400 works with the SAME port number and
the dev server needs no bind option. Linux permits the two binds because the
addresses differ; neither may be the wildcard address.

Two transports:
  - A port that answers HTTP goes through Caddy, which adds TLS and sets
    X-Forwarded-Proto. Rails compares the Origin header with its own base URL,
    so a bare TLS-to-TCP pipe would fail every form POST with a CSRF error.
  - Every other port (a database, a debugger) gets a raw TCP forward.
With no certificate in TLS_DIR, every port gets the raw forward, so plain
http:// still works.

A listener on the wildcard address (Docker publishes that way) is already
reachable, so it is left alone and gets no TLS.

A ROUTE sends one path prefix of a mirrored HTTP port to another local
upstream, in Caddy, before the port's own server sees it. A Rails app proxies
its Vite modules through its own Rack middleware, forty times slower than
Vite itself and four at a time; a route takes the modules straight from Vite
and the page keeps its same-origin URLs. A recipe declares routes in a
drop-in under /etc/sbx/mirror.d/:

    [[route]]
    port = 4400                      # a mirrored HTTP port
    path = "/vite-dev/*"             # a Caddy path matcher
    to   = "https://127.0.0.1:3036"  # https: TLS to the upstream, no verification (loopback)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tomllib
import urllib.request
from dataclasses import dataclass, field

TLS_DIR = "/etc/sbx/tls"
CONFIG_PATH = "/etc/sbx/mirror.toml"
DROPIN_DIR = "/etc/sbx/mirror.d"
CADDY_ADMIN = "http://127.0.0.1:2019"
POLL_SECONDS = 2.0
PROBE_TIMEOUT = 2.0
# A port that did not answer HTTP is probed again for this long, because a
# server can listen before it can answer.
REPROBE_WINDOW = 300.0
REPROBE_EVERY = 30.0

LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
WILDCARD = {"0.0.0.0", "::", "::ffff:0.0.0.0"}

# An UNKNOWN METHOD with a VALID version. Every HTTP server answers it with a
# status line (400, 405 or 501) before any application work; a real GET could
# take seconds on a cold Rails. The version must be valid: Python's http.server
# (so the Flask and Django dev servers) answers a bad version in HTTP/0.9
# style, with no status line at all, and would be classed as "not HTTP".
PROBE_BYTES = b"SBXPROBE / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"


@dataclass(frozen=True)
class Route:
    port: int
    path: str
    to: str        # "http://host:port" or "https://host:port"


@dataclass
class Settings:
    port_min: int = 1024
    port_max: int = 32767  # below the ephemeral range: tools bind port 0 up there
    # 2019 is the Caddy admin API: mirroring it hands out control of the proxy.
    # 9222 and 9229 are the Chrome and Node debug ports: remote code execution.
    skip: set[int] = field(default_factory=lambda: {2019, 9222, 9229})
    force_http: set[int] = field(default_factory=set)
    force_tcp: set[int] = field(default_factory=set)
    routes: list[Route] = field(default_factory=list)


def _read_toml(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log(f"settings {path}: {exc}")
        return {}


def _routes(raw: dict, source: str) -> list[Route]:
    out = []
    for r in raw.get("route", []):
        try:
            to = str(r["to"]).rstrip("/")
            if not to.startswith(("http://", "https://")):
                raise ValueError("`to` must start with http:// or https://")
            path = str(r["path"])
            if not path.startswith("/"):
                raise ValueError("`path` must start with /")
            out.append(Route(int(r["port"]), path, to))
        except (KeyError, TypeError, ValueError) as exc:
            log(f"route in {source} ignored: {exc}")
    return out


def load_settings(path: str = CONFIG_PATH, dropin_dir: str = DROPIN_DIR) -> Settings:
    """The main file, then every *.toml drop-in in name order. The lists add
    up; a scalar in a later file wins. A recipe owns one drop-in and rewrites
    it whole, so a second run of the recipe changes nothing."""
    s = Settings()
    files = [path]
    try:
        files += sorted(os.path.join(dropin_dir, f) for f in os.listdir(dropin_dir) if f.endswith(".toml"))
    except FileNotFoundError:
        pass
    skip = None
    for f in files:
        raw = _read_toml(f)
        if not raw:
            continue
        s.port_min = int(raw.get("port_min", s.port_min))
        s.port_max = int(raw.get("port_max", s.port_max))
        s.force_http |= set(raw.get("http", []))
        s.force_tcp |= set(raw.get("tcp", []))
        if "skip" in raw:
            skip = (skip or set()) | set(raw["skip"])
        s.routes += _routes(raw, f)
    if skip is not None:
        s.skip = skip
    # Naming a port in http/tcp is an explicit request, so it beats the skip list.
    s.skip = s.skip - s.force_http - s.force_tcp
    return s


# --- socket table -----------------------------------------------------------

def _hex_to_addr(hexaddr: str) -> str:
    """/proc/net/tcp stores an address as 32-bit words in HOST byte order."""
    raw = bytes.fromhex(hexaddr)
    words = [raw[i:i + 4][::-1] for i in range(0, len(raw), 4)]
    packed = b"".join(words)
    if len(packed) == 4:
        return socket.inet_ntop(socket.AF_INET, packed)
    return socket.inet_ntop(socket.AF_INET6, packed)


def parse_proc_net(text: str) -> list[tuple[str, int]]:
    """Every LISTEN socket in one /proc/net/tcp{,6} dump as (address, port)."""
    out = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 4 or cols[3] != "0A":  # 0A = TCP_LISTEN
            continue
        hexaddr, hexport = cols[1].rsplit(":", 1)
        out.append((_hex_to_addr(hexaddr), int(hexport, 16)))
    return out


def read_listeners() -> list[tuple[str, int]]:
    found = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as fh:
                found += parse_proc_net(fh.read())
        except FileNotFoundError:
            pass
    return found


def select_ports(listeners, settings: Settings, routed_ip: str,
                 mirrored: set[int]) -> dict[int, str]:
    """Ports to mirror, as {port: loopback address to dial}.

    `mirrored` is the set this service already serves. Its own listeners show
    up in the table as <routed_ip>:<port>; without the exclusion the service
    would see a non-loopback listener on the port, withdraw, and flap forever.
    """
    by_port: dict[int, set[str]] = {}
    for addr, port in listeners:
        if addr == routed_ip and port in mirrored:
            continue
        by_port.setdefault(port, set()).add(addr)

    chosen = {}
    for port, addrs in by_port.items():
        if port in settings.skip:
            continue
        forced = port in settings.force_http or port in settings.force_tcp
        if not forced and not settings.port_min <= port <= settings.port_max:
            continue
        loop = addrs & LOOPBACK
        # Any other address on the port means it is already reachable, or that
        # a bind on the routed address would collide with its owner.
        if not loop or addrs - LOOPBACK:
            continue
        chosen[port] = "127.0.0.1" if loop - {"::1"} else "::1"
    return chosen


def routed_ipv4() -> str | None:
    """The address of the default-route interface. No packet is sent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


# --- Caddy ------------------------------------------------------------------

def dial(addr: str, port: int) -> str:
    return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"


def _route_handler(route: Route) -> dict:
    scheme, _, hostport = route.to.partition("://")
    handler: dict = {"handler": "reverse_proxy", "upstreams": [{"dial": hostport}]}
    if scheme == "https":
        # The upstream is a dev server on loopback with a certificate for
        # some other name (Vite with the sandbox's own leaf, say): TLS, and
        # no verification, which on loopback gives away nothing.
        handler["transport"] = {"protocol": "http", "tls": {"insecure_skip_verify": True}}
    return handler


def caddy_config(routed_ip: str, http_ports: dict[int, str], tls_dir: str = TLS_DIR,
                 routes: list[Route] | tuple[Route, ...] = ()) -> dict:
    servers = {}
    for port, target in sorted(http_ports.items()):
        # A route's path is matched before the port's own upstream; Caddy
        # takes the first route that matches, so the catch-all comes last.
        port_routes = [{"match": [{"path": [r.path]}], "handle": [_route_handler(r)]}
                       for r in routes if r.port == port]
        servers[f"p{port}"] = {
            "listen": [f"{routed_ip}:{port}"],
            # http_redirect must precede tls: it answers a plain-HTTP request
            # on this port with a redirect to https, so both schemes work.
            "listener_wrappers": [{"wrapper": "http_redirect"}, {"wrapper": "tls"}],
            "automatic_https": {"disable": True},
            # No h3: it would add a UDP bind per port for no benefit here.
            "protocols": ["h1", "h2"],
            "tls_connection_policies": [{"certificate_selection": {"any_tag": ["sbx"]}}],
            "routes": port_routes + [{"handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": dial(target, port)}],
            }]}],
        }
    config: dict = {"admin": {"listen": "127.0.0.1:2019"}}
    if servers:
        config["apps"] = {
            "tls": {"certificates": {"load_files": [{
                "certificate": f"{tls_dir}/cert.pem",
                "key": f"{tls_dir}/key.pem",
                "tags": ["sbx"],
            }]}},
            "http": {"servers": servers},
        }
    return config


def _caddy(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CADDY_ADMIN + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        text = resp.read().decode()
    return json.loads(text) if text.strip() else None


def caddy_server_names() -> set[str]:
    try:
        servers = _caddy("GET", "/config/apps/http/servers")
    except Exception:
        return set()
    return set(servers or {})


# --- forwarding -------------------------------------------------------------

async def probe_http(addr: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(addr, port), PROBE_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.write(PROBE_BYTES)
        await writer.drain()
        head = await asyncio.wait_for(reader.read(8), PROBE_TIMEOUT)
        return head.startswith(b"HTTP/")
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        writer.close()


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def start_raw(routed_ip: str, port: int, target: str):
    async def handle(creader, cwriter):
        try:
            ureader, uwriter = await asyncio.open_connection(target, port)
        except OSError:
            cwriter.close()
            return
        await asyncio.gather(_pipe(creader, uwriter), _pipe(ureader, cwriter))

    return await asyncio.start_server(handle, host=routed_ip, port=port)


def log(msg: str):
    print(msg, flush=True)


async def main():
    raw: dict[int, asyncio.AbstractServer] = {}
    http: dict[int, str] = {}
    first_seen: dict[int, float] = {}
    last_probe: dict[int, float] = {}
    kind: dict[int, str] = {}
    posted = None
    bound_ip: str | None = None
    loop = asyncio.get_running_loop()

    while True:
        settings = load_settings()
        routed_ip = routed_ipv4()
        if routed_ip is None or routed_ip in LOOPBACK:
            await asyncio.sleep(POLL_SECONDS)
            continue

        # A new DHCP address strands every raw listener on the old one.
        if bound_ip is not None and bound_ip != routed_ip:
            for port in list(raw):
                raw.pop(port).close()
            log(f"routed address changed {bound_ip} -> {routed_ip}")
        bound_ip = routed_ip

        tls = os.path.exists(f"{TLS_DIR}/cert.pem") and os.path.exists(f"{TLS_DIR}/key.pem")
        wanted = select_ports(read_listeners(), settings, routed_ip, set(raw) | set(http))
        now = loop.time()

        for port in list(kind):
            if port not in wanted:
                kind.pop(port)
                first_seen.pop(port, None)
                last_probe.pop(port, None)

        for port, target in wanted.items():
            first_seen.setdefault(port, now)
            if port in settings.force_http:
                kind[port] = "http"
            elif port in settings.force_tcp:
                kind[port] = "tcp"
            elif kind.get(port) != "http":
                young = now - first_seen[port] < REPROBE_WINDOW
                due = now - last_probe.get(port, -1e9) >= REPROBE_EVERY
                if port not in kind or (young and due):
                    last_probe[port] = now
                    kind[port] = "http" if await probe_http(target, port) else "tcp"

        want_http = {p: t for p, t in wanted.items() if tls and kind[p] == "http"}
        want_raw = {p: t for p, t in wanted.items() if p not in want_http}

        for port in list(raw):
            if port not in want_raw:
                raw.pop(port).close()
                log(f"raw  -{port}")

        # Caddy first: a port that moves from raw to http must be released
        # above before Caddy binds it, and the reverse below.
        # The routes are part of the state: a recipe that writes a drop-in
        # while the port is already mirrored gets its route on the next poll.
        routes = tuple(r for r in settings.routes if r.port in want_http)
        state = (routed_ip, tuple(sorted(want_http.items())), routes)
        expected = {f"p{p}" for p in want_http}
        if state != posted or (expected and caddy_server_names() != expected):
            try:
                _caddy("POST", "/load", caddy_config(routed_ip, want_http, routes=routes))
                for r in routes:
                    if posted is None or r not in posted[2]:
                        log(f"http  {r.port}{r.path} -> {r.to}")
                for port in sorted(set(want_http) - set(http)):
                    log(f"http +{port} -> {dial(want_http[port], port)}")
                for port in sorted(set(http) - set(want_http)):
                    log(f"http -{port}")
                http = dict(want_http)
                posted = state
            except Exception as exc:  # Caddy down or a bind collision: retry next poll
                log(f"caddy load failed: {exc}")
                posted = None

        for port, target in want_raw.items():
            if port in raw:
                continue
            try:
                raw[port] = await start_raw(routed_ip, port, target)
                log(f"raw  +{port} -> {dial(target, port)}")
            except OSError as exc:
                log(f"raw  bind {routed_ip}:{port} failed: {exc}")

        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
