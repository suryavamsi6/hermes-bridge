"""Threaded stdlib HTTP server around :class:`hermes_bridge.app.Bridge`."""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from .app import MAX_BODY, Bridge

log = logging.getLogger("hermes_bridge")


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hermes-bridge"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 30  # seconds per socket read: a client that stalls mid-request can't pin a thread

        def _dispatch(self, method: str) -> None:
            parts = urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:  # rfile.read(-1) would read until the client closes
                return self._send(400, {"error": {"code": "bad_length", "message": "Invalid Content-Length."}})
            if length > MAX_BODY:
                return self._send(413, {"error": {"code": "body_too_large", "message": "Body too large."}})
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload = bridge.handle(method, parts.path, dict(parse_qsl(parts.query)), headers, body)
            self._send(status, payload)

        def _send(self, status: int, payload) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        do_GET = lambda self: self._dispatch("GET")        # noqa: E731
        do_POST = lambda self: self._dispatch("POST")      # noqa: E731
        do_PUT = lambda self: self._dispatch("PUT")        # noqa: E731
        do_DELETE = lambda self: self._dispatch("DELETE")  # noqa: E731

        def log_message(self, fmt, *args):  # never log headers (they carry the key)
            log.info("%s %s", self.command, urlsplit(self.path).path)

    return Handler


def serve(bridge: Bridge) -> None:
    httpd = ThreadingHTTPServer((bridge.cfg.host, bridge.cfg.port), make_handler(bridge))
    httpd.daemon_threads = True
    log.info("hermes-bridge listening on %s:%s -> %s", bridge.cfg.host, bridge.cfg.port, bridge.cfg.dashboard_url)
    httpd.serve_forever()
