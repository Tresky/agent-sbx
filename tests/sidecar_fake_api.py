#!/usr/bin/env python3
"""A stand-in for api.anthropic.com and api.github.com: answers every request
with the path and the credential headers it received, so a test can see which
token arrived. Usage: sidecar_fake_api.py <bind-address> <port>"""
import http.server
import json
import socketserver
import sys


class Echo(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def handle_any(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        data = json.dumps({"path": self.path, "authorization": self.headers.get("Authorization", ""),
                           "x-api-key": self.headers.get("x-api-key", "")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    Server((sys.argv[1], int(sys.argv[2])), Echo).serve_forever()
