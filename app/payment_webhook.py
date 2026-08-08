"""Small loopback HTTP listener for Robokassa ResultURL notifications."""

from __future__ import annotations

import logging
import secrets
import threading
from html import escape
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
        accepting_callbacks: bool = True,
        verify_duplicate_callback: bool = False,
        on_paid: Callable[[str], object] | None = None,
    ) -> None:
        self.service = service
        self.host = host
        self.port = port
        self.path = path
        self.accepting_callbacks = accepting_callbacks
        self.verify_duplicate_callback = verify_duplicate_callback
        self.on_paid = on_paid
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "RavunaPaymentWebhook/1"

            def log_message(self, format: str, *args: object) -> None:
                LOGGER.info("Payment webhook request completed")

            def _empty(self, status: int, *, location: str | None = None) -> None:
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _page(
                self,
                status: int,
                title: str,
                message: str,
                *,
                refresh_url: str | None = None,
                refresh_seconds: int = 2,
            ) -> None:
                refresh = (
                    f'<meta http-equiv="refresh" content="{refresh_seconds};url={escape(refresh_url, quote=True)}">'
                    if refresh_url else ""
                )
                action = (
                    f'<p><a href="{escape(refresh_url, quote=True)}">Обновить состояние</a></p>'
                    if refresh_url else ""
                )
                body = (
                    "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    f"{refresh}<title>{escape(title)}</title></head><body>"
                    f"<main><h1>{escape(title)}</h1><p>{escape(message)}</p>{action}</main>"
                    "</body></html>"
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def _payment_form(self, token: str) -> None:
                form = owner.service.payment_redirect_form(token)
                nonce = secrets.token_urlsafe(18)
                fields = "".join(
                    '<input type="hidden" name="{}" value="{}">'.format(
                        escape(name, quote=True), escape(value, quote=True)
                    )
                    for name, value in form.fields
                )
                body = (
                    '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>Переходим к оплате…</title></head><body>'
                    '<main><p>Переходим к безопасной оплате…</p>'
                    f'<form id="robokassa-payment" method="post" action="{escape(form.action_url, quote=True)}">'
                    f'{fields}<noscript><button type="submit">Перейти к оплате</button></noscript>'
                    '</form></main>'
                    f'<script nonce="{nonce}">document.getElementById("robokassa-payment").submit();</script>'
                    '</body></html>'
                ).encode("utf-8")
                action = urlsplit(form.action_url)
                action_origin = f"{action.scheme}://{action.netloc}"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; "
                    f"script-src 'nonce-{nonce}'; form-action {action_origin}; "
                    "base-uri 'none'; frame-ancestors 'none'",
                )
                self.end_headers()
                self.wfile.write(body)

            def _max_url(self, token: str, *, failed: bool = False) -> str:
                separator = "&" if "?" in owner.service.settings.max_bot_url else "?"
                prefix = "payfail_" if failed else "pay_"
                return f"{owner.service.settings.max_bot_url}{separator}start={prefix}{token}"

            def _handle_browser(self, parsed) -> bool:
                path = parsed.path.rstrip("/")
                routes = (
                    ("/p/", "checkout"),
                    ("/payment/success/", "success"),
                    ("/payment/fail/", "fail"),
                )
                route = next(((prefix, kind) for prefix, kind in routes if path.startswith(prefix)), None)
                if route is None:
                    return False
                if self.command != "GET":
                    self._empty(405)
                    return True
                prefix, kind = route
                token = path[len(prefix):]
                if "/" in token:
                    self._page(404, "Ссылка недоступна", "Проверьте ссылку и попробуйте снова.")
                    return True
                try:
                    order = owner.service.order_by_public_token(token)
                    if kind == "checkout":
                        self._payment_form(token)
                        return True
                except (PaymentError, PaymentUnavailable):
                    self._page(404, "Ссылка недоступна", "Срок действия ссылки истёк или она неверна.")
                    return True
                if kind == "fail":
                    self._empty(303, location=self._max_url(token, failed=True))
                    return True
                if order.status.value in {
                    "paid", "delivery_pending", "delivered", "partially_refunded"
                }:
                    self._empty(303, location=self._max_url(token))
                    return True
                if order.status.value in {"failed", "cancelled", "expired", "refunded"}:
                    self._empty(303, location=self._max_url(token, failed=True))
                    return True
                self._page(
                    200,
                    "Проверяем оплату…",
                    "Подтверждение от Robokassa ещё не получено.",
                    refresh_url=path,
                )
                return True

            def _handle(self) -> None:
                parsed = urlsplit(self.path)
                if self._handle_browser(parsed):
                    return
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
                if not owner.accepting_callbacks:
                    # Keep the public transport endpoint observable before the
                    # commercial gate opens, but never acknowledge or process a
                    # callback while the business webhook is disabled.
                    self.send_response(503)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Retry-After", "300")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
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
                if (
                    owner.verify_duplicate_callback
                    and result.accepted
                    and not result.duplicate
                ):
                    try:
                        duplicate = owner.service.process_webhook(
                            values,
                            method=self.command,
                            path=parsed.path,
                            source=self.client_address[0] if self.client_address else None,
                        )
                    except (PaymentError, PaymentUnavailable):
                        LOGGER.error("Sandbox duplicate callback probe failed safely")
                        self.send_error(503)
                        return
                    if not (
                        duplicate.accepted
                        and duplicate.duplicate
                        and duplicate.http_status == 200
                        and duplicate.response_text == result.response_text
                    ):
                        LOGGER.error("Sandbox duplicate callback probe invariant failed")
                        self.send_error(503)
                        return
                    LOGGER.info("Sandbox duplicate callback probe passed")
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
