#!/usr/bin/env python3
"""Local Saved Archive hub on 8891.

Serves the static Workers-ready shell in public/. Every route (/instagram,
/rednote, /threads, /telegram, /progress) renders the same shell; app.js loads
the matching backend URL from public/config.js in an iframe. This keeps the
frontend deployable to workers.dev while downloaders and progress stay separate
services.
"""

from __future__ import annotations

import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
PORT = int(os.environ.get("HUB_PORT", "8891"))
HOST = os.environ.get("HUB_HOST", "127.0.0.1")
ROUTES = {"/", "/instagram", "/rednote", "/threads", "/twitter", "/telegram", "/progress"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str,
              cors: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self._send(200, b'{"ok":true,"hub":"saved-archive"}',
                       "application/json", cors=True)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path in ROUTES or path.rstrip("/") in ROUTES:
            self._send(200, (PUBLIC / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
            return
        name = path.lstrip("/")
        if not name or "/" in name:
            self._send(404, b"not found", "text/plain")
            return
        target = PUBLIC / name
        if not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = {
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Saved Archive hub: http://{HOST}:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
