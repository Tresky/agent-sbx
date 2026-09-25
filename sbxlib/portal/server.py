"""`sbx web`: the portal's HTTP server, on 127.0.0.1 only.

The portal can make and destroy VMs, so a web page on another site must not
be able to drive it. Four locks:

  1. It listens on the loopback address only; no other machine reaches it.
  2. Every request needs the session token: `sbx web` prints a link with it,
     and the first visit turns it into an HttpOnly, SameSite=Strict cookie.
     A new token is made at each start.
  3. The Host header must name the loopback address and this port. A DNS
     name that a hostile site points at 127.0.0.1 (DNS rebinding) fails here.
  4. A request that changes something must carry an Origin header of this
     portal and a JSON body. A form on another site can send neither.

The page itself loads nothing from outside: no CDN, no font service. The
Content-Security-Policy says so to the browser too.
"""
from __future__ import annotations

import errno
import hmac
import http.client
import json
import mimetypes
import secrets
import signal
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import state_dir
from . import api
from .jobs import Busy, Jobs

STATIC = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"index.html", "app.js", "app.css", "icon.svg"}
MAX_BODY = 2_000_000
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def portal_dir() -> Path:
    return state_dir() / "portal"


class Portal(ThreadingHTTPServer):
    daemon_threads = True
    # Refuse a second server on the port, instead of sharing it (SO_REUSEPORT).
    allow_reuse_address = False

    def __init__(self, port: int, ctx: api.Context, token: str | None = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.ctx = ctx
        self.token = token or secrets.token_urlsafe(32)
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self.cookie = f"sbx_portal_{self.port}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: Portal
    server_version = "sbx-portal"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # the Activity page is the log; the terminal stays quiet

    # --- replies ---
    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def _text_page(self, status: int, title: str, text: str):
        body = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                f"<body><h1>{title}</h1><p>{text}</p>")
        self._send(status, body.encode(), "text/html; charset=utf-8")

    # --- the locks ---
    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.hosts

    def _authed(self) -> bool:
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == self.server.cookie and hmac.compare_digest(value.encode(), self.server.token.encode()):
                return True
        return False

    def _origin_ok(self) -> bool:
        return self.headers.get("Origin", "") in self.server.origins

    # --- dispatch ---
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if not self._host_ok():
            return self._text_page(403, "Wrong address", "Open the portal at the link that <code>sbx web</code> printed.")
        if method == "GET" and url.path == "/" and "t" in query:
            if hmac.compare_digest(query["t"].encode(), self.server.token.encode()):
                # The page itself, not a redirect: Safari drops a Strict cookie
                # that a redirect sets. app.js takes the token out of the address.
                cookie = f"{self.server.cookie}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8",
                                  {"Set-Cookie": cookie})
            return self._text_page(403, "Old link", "This link is from an earlier start of the portal. "
                                                    "Run <code>sbx web</code> again; it opens the current link.")
        if not self._authed():
            if url.path.startswith("/api/"):
                return self._json(401, {"error": "not signed in to the portal; run `sbx web` again"})
            return self._text_page(401, "Sign in", "Open the portal with the link that <code>sbx web</code> "
                                                   "printed. Run <code>sbx web</code> again to open it.")
        if method != "GET":
            if not self._origin_ok():
                return self._json(403, {"error": "the request did not come from the portal's own page"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "a change needs a JSON body"})
        if url.path.startswith("/api/"):
            return self._api(method, url.path, query)
        if method != "GET":
            return self._json(405, {"error": "not allowed"})
        return self._static(url.path)

    def _static(self, path: str):
        name = "index.html" if path in ("/", "/index.html") else path.removeprefix("/static/")
        if name not in STATIC_FILES:
            return self._text_page(404, "Not found", "No such page.")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, (STATIC / name).read_bytes(), ctype)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise api.HttpError(413, "the request is too large")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            raise api.HttpError(400, "the body is not JSON") from None
        if not isinstance(data, dict):
            raise api.HttpError(400, "the body must be a JSON object")
        return data

    def _api(self, method: str, path: str, query: dict):
        try:
            fn, kwargs = api.route(method, path)
            req = Request(query, self._body() if method != "GET" else {})
            result = fn(self.server.ctx, req, **kwargs)
        except api.HttpError as exc:
            return self._json(exc.status, {"error": str(exc)})
        except Busy as exc:
            return self._json(409, {"error": str(exc)})
        except api.USER_ERRORS as exc:
            return self._json(400, {"error": str(exc)})
        except api.REMOTE_ERRORS as exc:
            return self._json(502, {"error": str(exc)})
        except (ConnectionError, BrokenPipeError):
            return
        except Exception as exc:  # noqa: BLE001 - one broken route must not stop the portal
            traceback.print_exc()
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        if isinstance(result, tuple):
            # A download: (content type, text, file name).
            ctype, text, filename = result
            return self._send(200, text.encode(), ctype,
                              {"Content-Disposition": f'attachment; filename="{filename}"'})
        return self._json(200, result)


class Request:
    def __init__(self, query: dict, body: dict):
        self.query, self.body = query, body


def _running_portal(port: int) -> str | None:
    """The link of a portal that already answers on `port`, from its file."""
    link = portal_dir() / "url"
    try:
        url = link.read_text().strip()
    except OSError:
        return None
    parsed = urllib.parse.urlsplit(url)
    token = dict(urllib.parse.parse_qsl(parsed.query)).get("t", "")
    if parsed.port != port or not token:
        return None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/api/ping", headers={"Host": f"127.0.0.1:{port}",
                                                  "Cookie": f"sbx_portal_{port}={token}"})
        return url if conn.getresponse().status == 200 else None
    except OSError:
        return None
    finally:
        conn.close()


def serve(port: int, open_browser: bool = True, ctx: api.Context | None = None) -> int:
    ctx = ctx or api.Context(Jobs(portal_dir() / "jobs"))
    try:
        server = Portal(port, ctx)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        url = _running_portal(port)
        if url is None:
            print(f"sbx: error: port {port} is in use by another program; pass --port <another>", file=sys.stderr)
            return 1
        print(f"the portal runs already: {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    link = portal_dir() / "url"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.parent.chmod(0o700)
    link.write_text(server.url + "\n")
    link.chmod(0o600)
    print(f"the sbx portal: {server.url}", flush=True)
    print("It listens on this Mac only. Keep this window open; Ctrl-C stops the portal.", flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(server.url,)).start()

    def on_term(signum, frame):
        raise KeyboardInterrupt  # the same clean end as Ctrl-C

    signal.signal(signal.SIGTERM, on_term)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        link.unlink(missing_ok=True)
        stopped = ctx.jobs.stop_all()
        if stopped:
            print("stopped the jobs that still ran: " + ", ".join(stopped))
    print("the portal stopped")
    return 0
