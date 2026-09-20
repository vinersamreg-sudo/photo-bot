"""Fast loopback webhook receiver; work is always deferred to the durable queue."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import AvitoResponderService


LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 128 * 1024


class AvitoWebhookServer:
    def __init__(self, service: AvitoResponderService, host: str, port: int, path: str) -> None:
        self.service = service
        self.host = host
        self.port = port
        self.path = path
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def build(self) -> ThreadingHTTPServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "RavunaAvitoWebhook/1"

            def log_message(self, format: str, *args: object) -> None:
                LOGGER.info("Avito webhook request completed")

            def do_POST(self) -> None:  # noqa: N802
                if self.path != owner.path:
                    self._reply(404, {"ok": False})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._reply(400, {"ok": False})
                    return
                if length <= 0 or length > MAX_BODY_BYTES:
                    self._reply(413, {"ok": False})
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = owner.service.accept_webhook(payload)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                    self._reply(400, {"ok": False})
                    return
                self._reply(200, {"ok": True, "duplicate": result["duplicate"]})

            def do_GET(self) -> None:  # noqa: N802
                self._reply(405, {"ok": False})

            def do_PUT(self) -> None:  # noqa: N802
                self._reply(405, {"ok": False})

            def do_DELETE(self) -> None:  # noqa: N802
                self._reply(405, {"ok": False})

            def _reply(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        server.daemon_threads = True
        return server

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = self.build()
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="avito-webhook", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
