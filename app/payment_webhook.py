"""Small loopback HTTP listener for Robokassa ResultURL notifications."""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

from app.payments import PaymentError, PaymentService, PaymentUnavailable


LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 64 * 1024


class PaymentWebhookServer:
    """Runs behind the host HTTPS reverse proxy; never serves public TLS itself."""

    def __init__(
        self,
        service: PaymentService,
        host: str,
        port: int,
        path: str,
        *,
        on_paid: Callable[[str], object] | None = None,
    ) -> None:
        self.service = service
        self.host = host
        self.port = port
        self.path = path
        self.on_paid = on_paid
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PixoraPaymentWebhook/1"

            def log_message(self, format: str, *args: object) -> None:
                LOGGER.info("Payment webhook request completed")

            def _handle(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path != owner.path:
                    self.send_error(404)
                    return
                if self.command != "POST":
                    self.send_response(405)
                    self.send_header("Allow", "POST")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                values = dict(parse_qsl(parsed.query, keep_blank_values=True))
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if length < 0 or length > MAX_BODY_BYTES:
                    self.send_error(413)
                    return
                try:
                    body = self.rfile.read(length).decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    self.send_error(400)
                    return
                values.update(dict(parse_qsl(body, keep_blank_values=True)))
                try:
                    result = owner.service.process_webhook(
                        values,
                        method=self.command,
                        path=parsed.path,
                        source=self.client_address[0] if self.client_address else None,
                    )
                except (PaymentError, PaymentUnavailable):
                    LOGGER.warning("Payment webhook rejected safely")
                    self.send_error(503)
                    return
                if result.accepted and not result.duplicate and result.order_id and owner.on_paid:
                    try:
                        owner.on_paid(result.order_id)
                    except Exception as exc:
                        # Payment acknowledgement and ledger grant remain authoritative.
                        # The user notification is retriable and grants nothing itself.
                        LOGGER.error(
                            "Paid package notification deferred (error_type=%s)",
                            type(exc).__name__,
                        )
                response = result.response_text.encode("utf-8")
                self.send_response(result.http_status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(response)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                self._handle()

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                self._handle()

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="payment-webhook",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info("Payment webhook listener started (host=%s,port=%s)", self.host, self.port)

    @property
    def bound_port(self) -> int:
        if self._server is None:
            raise RuntimeError("Payment webhook listener is not running")
        return int(self._server.server_address[1])

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        LOGGER.info("Payment webhook listener stopped")
