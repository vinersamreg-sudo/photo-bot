"""Opt-in MAX ingress; acknowledge only a committed inbox entry."""

from __future__ import annotations

import hmac
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.database import Database
from app.max_transport import MaxIncomingEvent, parse_update

LOGGER = logging.getLogger(__name__)
PATH = "/webhooks/max"
MAX_BYTES = 128 * 1024
MAX_PENDING = 256
MAX_AGE = 15 * 60


class MaxInbox:
    def __init__(self, database: Database):
        self.db = database
        with self.db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS max_webhook_inbox(
                seq INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL, payload TEXT, received INTEGER NOT NULL,
                claimed INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at INTEGER NOT NULL DEFAULT 0, failed_at INTEGER)""")

    def accept(self, event, payload: str) -> bool:
        with self.db.read() as conn:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("PRAGMA secure_delete=ON")
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                    """SELECT 1 FROM max_webhook_inbox WHERE event_key=?
                       UNION ALL SELECT 1 FROM max_processed_events
                       WHERE event_key=? AND status='completed'""",
                    (event.event_key, event.event_key),
                ).fetchone():
                    return True
                if conn.execute("SELECT count(*) FROM max_webhook_inbox WHERE payload IS NOT NULL").fetchone()[0] >= MAX_PENDING:
                    return False
                conn.execute(
                    "INSERT INTO max_webhook_inbox(event_key,event_type,payload,received) VALUES(?,?,?,?)",
                    (event.event_key, event.event_type, payload, int(time.time())),
                )
        return True

    def recover(self) -> None:
        # A claimed event may already have performed an external effect. Don't
        # blindly run it again after an ambiguous interruption. Keep its receipt.
        with self.db.transaction() as conn:
            conn.execute("PRAGMA secure_delete=ON")
            n = conn.execute(
                "UPDATE max_webhook_inbox SET payload=NULL,failed_at=? WHERE claimed=1 AND payload IS NOT NULL",
                (int(time.time()),),
            ).rowcount
        if n:
            LOGGER.error("MAX webhook interrupted claims quarantined (count=%s)", n)

    def ready(self) -> bool:
        with self.db.read() as conn:
            conn.execute("PRAGMA busy_timeout=500")
            oldest, count = conn.execute(
                "SELECT min(received), count(*) FROM max_webhook_inbox WHERE payload IS NOT NULL"
            ).fetchone()
            recent_error = conn.execute(
                "SELECT 1 FROM max_webhook_inbox WHERE failed_at>? LIMIT 1", (int(time.time()) - 900,),
            ).fetchone()
        return not recent_error and count < MAX_PENDING and (oldest is None or time.time() - oldest < 180)

    def take(self):
        now = int(time.time())
        with self.db.transaction() as conn:
            conn.execute("PRAGMA secure_delete=ON")
            expired = conn.execute(
                "UPDATE max_webhook_inbox SET payload=NULL,failed_at=? WHERE payload IS NOT NULL AND received<?",
                (now, now - MAX_AGE),
            ).rowcount
            # Minimal receipts outlast MAX's documented retry horizon.
            conn.execute(
                "DELETE FROM max_webhook_inbox WHERE payload IS NULL AND received<?",
                (now - 7 * 86400,),
            )
            row = conn.execute(
                """SELECT seq,payload,event_key,attempts,retry_at FROM max_webhook_inbox
                   WHERE claimed=0 AND payload IS NOT NULL ORDER BY seq LIMIT 1"""
            ).fetchone()
            if row and row["retry_at"] > now:
                row = None
            if row:
                conn.execute("UPDATE max_webhook_inbox SET claimed=1,attempts=attempts+1 WHERE seq=?", (row["seq"],))
        if expired:
            LOGGER.error("MAX webhook events expired before processing (count=%s)", expired)
        return row

    def finish(self, seq: int, failed=False) -> None:
        with self.db.transaction() as conn:
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("UPDATE max_webhook_inbox SET payload=NULL,failed_at=? WHERE seq=?",
                         (int(time.time()) if failed else None, seq))

    def retry_if_unclaimed(self, row):
        with self.db.transaction() as conn:
            if row["attempts"] >= 2 or conn.execute(
                "SELECT 1 FROM max_processed_events WHERE event_key=?", (row["event_key"],)
            ).fetchone():
                return False
            conn.execute("UPDATE max_webhook_inbox SET claimed=0,retry_at=? WHERE seq=?",
                         (int(time.time()) + 2 ** (row["attempts"] + 1), row["seq"]))
            return True

    def handle_next(self, application) -> bool:
        row = self.take()
        if row is None:
            return False
        try:
            data = json.loads(row["payload"])
            data["image_urls"] = tuple(data["image_urls"])
            event = MaxIncomingEvent(**data)
            application.handle(event)
        except Exception as exc:
            from app.max_runtime import _is_permanent_recipient_failure
            from app.max_transport import MaxTransportError

            if isinstance(exc, MaxTransportError) and _is_permanent_recipient_failure(exc):
                application.store.finish_event(row["event_key"], True)
                self.finish(row["seq"])
                return True
            if self.retry_if_unclaimed(row):
                LOGGER.warning("MAX webhook retry before application claim (error_type=%s)", type(exc).__name__)
                return True
            # The app has commerce idempotency, but an outbound MAX request
            # may already have succeeded. Alert rather than repeat effects.
            LOGGER.error("MAX webhook handler outcome uncertain (error_type=%s)", type(exc).__name__)
            self.finish(row["seq"], failed=True)
            return True
        self.finish(row["seq"])
        return True


def valid_secret(secret: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret) is not None


class MaxWebhookServer:
    def __init__(self, inbox: MaxInbox, port: int, secret: str):
        if not valid_secret(secret):
            raise ValueError("MAX webhook requires a dedicated strong secret")
        encoded_secret = secret.encode("ascii")

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(5)
                super().setup()

            def log_message(self, *_args):
                pass

            def reply(self, status):
                self.send_response_only(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if self.path != "/readyz":
                    return self.reply(404)
                try:
                    return self.reply(200 if inbox.ready() else 503)
                except sqlite3.Error:
                    return self.reply(503)

            def do_POST(self):
                if self.path != PATH:
                    return self.reply(404)
                headers = self.headers.get_all("X-Max-Bot-Api-Secret", [])
                if len(headers) != 1 or not hmac.compare_digest(headers[0].encode("utf-8"), encoded_secret):
                    return self.reply(403)
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
                    return self.reply(400)
                length = int(lengths[0])
                if not 0 < length <= MAX_BYTES:
                    return self.reply(413)
                if self.headers.get_content_type() != "application/json":
                    return self.reply(415)
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        return self.reply(400)
                    update = json.loads(raw.decode("utf-8"))
                    event = parse_update(update)
                    if event is None:
                        if update.get("update_type") in {"bot_started", "message_created", "message_callback"}:
                            return self.reply(400)
                        return self.reply(200)
                    if any(not isinstance(value, str) for value in event.image_urls) or (
                        event.text is not None and not isinstance(event.text, str)
                    ):
                        return self.reply(400)
                    if not inbox.accept(event, json.dumps(asdict(event), ensure_ascii=False)):
                        return self.reply(503)
                except (TypeError, ValueError, AttributeError, OverflowError, RecursionError):
                    return self.reply(400)
                except sqlite3.Error:
                    LOGGER.error("MAX webhook could not commit an inbox entry")
                    return self.reply(503)
                return self.reply(200)

        class QuietServer(HTTPServer):
            def handle_error(self, *_args):
                LOGGER.warning("MAX webhook request disconnected or failed")

        self.server = QuietServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join()
        self.server.server_close()
