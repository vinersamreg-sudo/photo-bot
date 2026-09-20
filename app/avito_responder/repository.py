"""Durable debounce, leases, idempotency and diagnostics for Avito replies."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ClaimedChat, IncomingEvent, SendingAttempt


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT '',
    inbound_valid INTEGER NOT NULL DEFAULT 0,
    allowlist_pass INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_jobs (
    chat_id TEXT PRIMARY KEY,
    item_id INTEGER NOT NULL,
    last_message_id TEXT NOT NULL,
    last_inbound_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    lock_token TEXT,
    lock_until TEXT,
    first_reply_sent_at TEXT,
    automation_closed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reply_attempts (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL UNIQUE,
    source_revision INTEGER NOT NULL,
    reply_text TEXT,
    model TEXT,
    status TEXT NOT NULL,
    provider_response_id TEXT,
    avito_message_id TEXT,
    error_class TEXT,
    send_attempts INTEGER NOT NULL DEFAULT 0,
    bundle_message_count INTEGER NOT NULL DEFAULT 0,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    context_fetched_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class AvitoRepository:
    def __init__(
        self,
        path: Path,
        *,
        debounce_seconds: int = 8,
        lease_seconds: int = 60,
    ) -> None:
        self.path = path
        self.debounce_seconds = debounce_seconds
        self.lease_seconds = lease_seconds

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.path.parent.chmod(0o700)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            attempt_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(reply_attempts)")
            }
            for name, definition in (
                ("send_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("bundle_message_count", "INTEGER NOT NULL DEFAULT 0"),
                ("attachment_count", "INTEGER NOT NULL DEFAULT 0"),
                ("context_fetched_at", "TEXT"),
            ):
                if name not in attempt_columns:
                    connection.execute(f"ALTER TABLE reply_attempts ADD COLUMN {name} {definition}")
            event_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(webhook_events)")
            }
            for name, definition in (
                ("direction", "TEXT NOT NULL DEFAULT ''"),
                ("inbound_valid", "INTEGER NOT NULL DEFAULT 0"),
                ("allowlist_pass", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in event_columns:
                    connection.execute(f"ALTER TABLE webhook_events ADD COLUMN {name} {definition}")
            connection.commit()
        if os.name != "nt":
            self.path.chmod(0o600)

    def record_event(
        self,
        event: IncomingEvent,
        *,
        eligible: bool,
        inbound_valid: bool = False,
        allowlist_pass: bool = False,
        reopen_observed: bool = False,
        now: datetime | None = None,
    ) -> tuple[bool, bool]:
        received = _utc(now)
        due = received + timedelta(seconds=self.debounce_seconds)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """INSERT OR IGNORE INTO webhook_events(
                       event_id,message_id,chat_id,item_id,received_at,status,
                       direction,inbound_valid,allowlist_pass
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id,
                    event.message_id,
                    event.chat_id,
                    event.item_id,
                    received.isoformat(),
                    "pending" if eligible else "ignored",
                    event.direction,
                    int(inbound_valid),
                    int(allowlist_pass),
                ),
            ).rowcount
            scheduled = 0
            if inserted and eligible:
                scheduled = connection.execute(
                    """INSERT INTO chat_jobs(
                           chat_id,item_id,last_message_id,last_inbound_at,due_at,
                           revision,status,updated_at
                       ) VALUES(?,?,?,?,?,1,'pending',?)
                       ON CONFLICT(chat_id) DO UPDATE SET
                           item_id=excluded.item_id,
                           last_message_id=excluded.last_message_id,
                           last_inbound_at=excluded.last_inbound_at,
                           due_at=excluded.due_at,
                           revision=chat_jobs.revision+1,
                           status=CASE
                               WHEN chat_jobs.status IN ('processing','sending')
                                   THEN chat_jobs.status
                               ELSE 'pending'
                           END,
                           lock_token=CASE
                               WHEN chat_jobs.status IN ('processing','sending')
                                   THEN chat_jobs.lock_token
                               ELSE NULL
                           END,
                           lock_until=CASE
                               WHEN chat_jobs.status IN ('processing','sending')
                                   THEN chat_jobs.lock_until
                               ELSE NULL
                           END,
                           updated_at=excluded.updated_at
                       WHERE chat_jobs.first_reply_sent_at IS NULL
                         AND chat_jobs.automation_closed_at IS NULL
                         AND (chat_jobs.status!='observed' OR ?)""",
                    (
                        event.chat_id,
                        event.item_id,
                        event.message_id,
                        received.isoformat(),
                        due.isoformat(),
                        received.isoformat(),
                        int(reopen_observed),
                    ),
                ).rowcount
                if not scheduled:
                    connection.execute(
                        "UPDATE webhook_events SET status='ignored' WHERE event_id=?",
                        (event.event_id,),
                    )
            connection.commit()
        return bool(inserted), bool(scheduled)

    def claim_due(self, *, now: datetime | None = None) -> ClaimedChat | None:
        current = _utc(now)
        token = uuid.uuid4().hex
        lock_until = current + timedelta(seconds=self.lease_seconds)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = tuple(
                str(row[0])
                for row in connection.execute(
                    """SELECT chat_id FROM chat_jobs
                       WHERE status='processing' AND first_reply_sent_at IS NULL
                         AND automation_closed_at IS NULL
                         AND lock_until IS NOT NULL AND lock_until<=?""",
                    (current.isoformat(),),
                ).fetchall()
            )
            if expired:
                placeholders = ",".join("?" for _ in expired)
                connection.execute(
                    f"DELETE FROM reply_attempts WHERE status='prepared' AND chat_id IN ({placeholders})",
                    expired,
                )
                connection.execute(
                    f"""UPDATE chat_jobs SET status='pending',lock_token=NULL,lock_until=NULL,
                               updated_at=? WHERE chat_id IN ({placeholders})""",
                    (current.isoformat(), *expired),
                )
            row = connection.execute(
                """SELECT chat_id,item_id,revision,last_message_id FROM chat_jobs
                   WHERE status='pending' AND first_reply_sent_at IS NULL
                     AND automation_closed_at IS NULL AND due_at<=?
                   ORDER BY due_at LIMIT 1""",
                (current.isoformat(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """UPDATE chat_jobs SET status='processing',lock_token=?,lock_until=?,updated_at=?
                   WHERE chat_id=? AND revision=? AND status='pending'
                     AND first_reply_sent_at IS NULL AND automation_closed_at IS NULL""",
                (
                    token,
                    lock_until.isoformat(),
                    current.isoformat(),
                    row[0],
                    row[2],
                ),
            ).rowcount
            connection.commit()
        if not updated:
            return None
        return ClaimedChat(str(row[0]), int(row[1]), int(row[2]), str(row[3]), token)

    def revision_is_current(self, claim: ClaimedChat) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT revision,status,lock_token,first_reply_sent_at,automation_closed_at
                   FROM chat_jobs WHERE chat_id=?""",
                (claim.chat_id,),
            ).fetchone()
        return bool(
            row
            and int(row[0]) == claim.revision
            and row[1] == "processing"
            and row[2] == claim.lock_token
            and row[3] is None
            and row[4] is None
        )

    def renew_processing_lease(
        self, claim: ClaimedChat, *, now: datetime | None = None
    ) -> bool:
        current = _utc(now)
        lock_until = current + timedelta(seconds=self.lease_seconds)
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """UPDATE chat_jobs SET lock_until=?,updated_at=?
                   WHERE chat_id=? AND revision=? AND status='processing'
                     AND lock_token=? AND first_reply_sent_at IS NULL
                     AND automation_closed_at IS NULL""",
                (
                    lock_until.isoformat(),
                    current.isoformat(),
                    claim.chat_id,
                    claim.revision,
                    claim.lock_token,
                ),
            ).rowcount
        return bool(changed)

    def prepare_reply(
        self,
        claim: ClaimedChat,
        text: str,
        model: str,
        provider_response_id: str | None,
        *,
        bundle_message_count: int = 0,
        attachment_count: int = 0,
        now: datetime | None = None,
    ) -> bool:
        timestamp = _utc(now).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT revision,status,lock_token,first_reply_sent_at,automation_closed_at
                   FROM chat_jobs WHERE chat_id=?""",
                (claim.chat_id,),
            ).fetchone()
            if (
                not current
                or int(current[0]) != claim.revision
                or current[1] != "processing"
                or current[2] != claim.lock_token
                or current[3]
                or current[4]
            ):
                connection.commit()
                return False
            inserted = connection.execute(
                """INSERT INTO reply_attempts(
                       id,chat_id,source_revision,reply_text,model,status,
                       provider_response_id,send_attempts,bundle_message_count,
                       attachment_count,context_fetched_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'prepared',?,0,?,?,?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                       id=excluded.id,
                       source_revision=excluded.source_revision,
                       reply_text=excluded.reply_text,
                       model=excluded.model,
                       status='prepared',
                       provider_response_id=excluded.provider_response_id,
                       avito_message_id=NULL,
                       error_class=NULL,
                       send_attempts=0,
                       bundle_message_count=excluded.bundle_message_count,
                       attachment_count=excluded.attachment_count,
                       context_fetched_at=excluded.context_fetched_at,
                       created_at=excluded.created_at,
                       updated_at=excluded.updated_at
                   WHERE reply_attempts.status='observed'
                     AND reply_attempts.source_revision<excluded.source_revision""",
                (
                    uuid.uuid4().hex,
                    claim.chat_id,
                    claim.revision,
                    text,
                    model,
                    provider_response_id,
                    bundle_message_count,
                    attachment_count,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            ).rowcount
            connection.commit()
        return bool(inserted)

    def mark_observed(
        self, claim: ClaimedChat, *, now: datetime | None = None
    ) -> bool:
        timestamp = _utc(now).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            job_changed = connection.execute(
                """UPDATE chat_jobs SET status='observed',lock_token=NULL,lock_until=NULL,
                          updated_at=? WHERE chat_id=? AND revision=? AND status='processing'
                          AND lock_token=? AND first_reply_sent_at IS NULL
                          AND automation_closed_at IS NULL""",
                (timestamp, claim.chat_id, claim.revision, claim.lock_token),
            ).rowcount
            attempt_changed = 0
            if job_changed:
                attempt_changed = connection.execute(
                    """UPDATE reply_attempts SET status='observed',updated_at=?
                       WHERE chat_id=? AND source_revision=? AND status='prepared'
                         AND send_attempts=0""",
                    (timestamp, claim.chat_id, claim.revision),
                ).rowcount
            if not attempt_changed:
                connection.rollback()
                return False
            connection.execute(
                "UPDATE webhook_events SET status='observed' WHERE chat_id=? AND status='pending'",
                (claim.chat_id,),
            )
            connection.commit()
        return True

    def mark_sending(
        self, claim: ClaimedChat, *, now: datetime | None = None
    ) -> bool:
        current = _utc(now)
        lock_until = current + timedelta(seconds=self.lease_seconds)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            job_changed = connection.execute(
                """UPDATE chat_jobs SET status='sending',lock_until=?,updated_at=?
                   WHERE chat_id=? AND revision=? AND status='processing'
                     AND lock_token=? AND first_reply_sent_at IS NULL
                     AND automation_closed_at IS NULL""",
                (
                    lock_until.isoformat(),
                    current.isoformat(),
                    claim.chat_id,
                    claim.revision,
                    claim.lock_token,
                ),
            ).rowcount
            attempt_changed = 0
            if job_changed:
                attempt_changed = connection.execute(
                    """UPDATE reply_attempts SET status='sending',send_attempts=send_attempts+1,
                              updated_at=? WHERE chat_id=? AND source_revision=?
                              AND status='prepared'""",
                    (current.isoformat(), claim.chat_id, claim.revision),
                ).rowcount
            if not attempt_changed:
                connection.rollback()
                return False
            connection.commit()
        return True

    def recoverable_sending(
        self, *, now: datetime | None = None
    ) -> SendingAttempt | None:
        current = _utc(now)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT a.chat_id,a.reply_text,a.source_revision,j.revision,j.last_message_id,
                          a.send_attempts,a.updated_at
                   FROM reply_attempts a JOIN chat_jobs j ON j.chat_id=a.chat_id
                   WHERE a.status='sending' AND j.status='sending'
                     AND j.first_reply_sent_at IS NULL AND j.automation_closed_at IS NULL
                     AND j.lock_until IS NOT NULL AND j.lock_until<=?
                   ORDER BY a.updated_at LIMIT 1""",
                (current.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        return SendingAttempt(
            chat_id=str(row[0]),
            reply_text=str(row[1] or ""),
            source_revision=int(row[2]),
            current_revision=int(row[3]),
            last_message_id=str(row[4]),
            send_attempts=int(row[5]),
            updated_at=datetime.fromisoformat(str(row[6])).astimezone(timezone.utc),
        )

    def claim_reconciled_retry(
        self, attempt: SendingAttempt, *, now: datetime | None = None
    ) -> str | None:
        current = _utc(now)
        token = uuid.uuid4().hex
        lock_until = current + timedelta(seconds=self.lease_seconds)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE chat_jobs SET lock_token=?,lock_until=?,updated_at=?
                   WHERE chat_id=? AND status='sending' AND lock_until<=?
                     AND first_reply_sent_at IS NULL AND automation_closed_at IS NULL""",
                (
                    token,
                    lock_until.isoformat(),
                    current.isoformat(),
                    attempt.chat_id,
                    current.isoformat(),
                ),
            ).rowcount
            if changed:
                changed = connection.execute(
                    """UPDATE reply_attempts SET send_attempts=send_attempts+1,updated_at=?
                       WHERE chat_id=? AND status='sending' AND send_attempts=1""",
                    (current.isoformat(), attempt.chat_id),
                ).rowcount
            if not changed:
                connection.rollback()
                return None
            connection.commit()
        return token

    def mark_sent(
        self, chat_id: str, avito_message_id: str, *, now: datetime | None = None
    ) -> None:
        timestamp = _utc(now).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE reply_attempts SET status='sent',avito_message_id=?,updated_at=?
                   WHERE chat_id=? AND status IN ('prepared','sending')""",
                (avito_message_id, timestamp, chat_id),
            )
            connection.execute(
                """UPDATE chat_jobs SET status='sent',first_reply_sent_at=?,lock_token=NULL,
                       lock_until=NULL,updated_at=? WHERE chat_id=? AND first_reply_sent_at IS NULL""",
                (timestamp, timestamp, chat_id),
            )
            connection.execute(
                "UPDATE webhook_events SET status='completed' WHERE chat_id=? AND status='pending'",
                (chat_id,),
            )
            connection.commit()

    def mark_terminal(
        self,
        chat_id: str,
        status: str,
        error_class: str | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        timestamp = _utc(now).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE reply_attempts SET status=?,error_class=?,updated_at=?
                   WHERE chat_id=? AND status!='sent'""",
                (status, error_class, timestamp, chat_id),
            )
            connection.execute(
                """UPDATE chat_jobs SET status=?,automation_closed_at=?,lock_token=NULL,
                       lock_until=NULL,updated_at=?
                   WHERE chat_id=? AND first_reply_sent_at IS NULL""",
                (status, timestamp, timestamp, chat_id),
            )
            connection.execute(
                "UPDATE webhook_events SET status=? WHERE chat_id=? AND status='pending'",
                (status, chat_id),
            )
            connection.commit()

    def mark_claim_terminal(
        self,
        claim: ClaimedChat,
        status: str,
        error_class: str | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        timestamp = _utc(now).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE chat_jobs SET status=?,automation_closed_at=?,lock_token=NULL,
                          lock_until=NULL,updated_at=?
                   WHERE chat_id=? AND revision=? AND lock_token=?
                     AND status IN ('processing','sending')
                     AND first_reply_sent_at IS NULL AND automation_closed_at IS NULL""",
                (
                    status,
                    timestamp,
                    timestamp,
                    claim.chat_id,
                    claim.revision,
                    claim.lock_token,
                ),
            ).rowcount
            if changed:
                connection.execute(
                    """UPDATE reply_attempts SET status=?,error_class=?,updated_at=?
                       WHERE chat_id=? AND source_revision=? AND status!='sent'""",
                    (status, error_class, timestamp, claim.chat_id, claim.revision),
                )
                connection.execute(
                    "UPDATE webhook_events SET status=? WHERE chat_id=? AND status='pending'",
                    (status, claim.chat_id),
                )
            connection.commit()
        return bool(changed)

    def release_for_newer_revision(
        self, claim: ClaimedChat, *, now: datetime | None = None
    ) -> None:
        timestamp = _utc(now).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE chat_jobs SET status='pending',lock_token=NULL,lock_until=NULL,
                          updated_at=? WHERE chat_id=? AND status='processing'
                          AND lock_token=? AND first_reply_sent_at IS NULL""",
                (timestamp, claim.chat_id, claim.lock_token),
            ).rowcount
            if changed:
                connection.execute(
                    """DELETE FROM reply_attempts WHERE chat_id=? AND source_revision=?
                       AND status='prepared'""",
                    (claim.chat_id, claim.revision),
                )
            connection.commit()

    def status(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) FROM chat_jobs GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def cleanup(self, cutoff: datetime) -> dict[str, int]:
        before = cutoff.astimezone(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempts = connection.execute(
                "DELETE FROM reply_attempts WHERE updated_at<? AND status NOT IN ('sending','prepared')",
                (before,),
            ).rowcount
            events = connection.execute(
                "DELETE FROM webhook_events WHERE received_at<? AND status!='pending'",
                (before,),
            ).rowcount
            jobs = connection.execute(
                """DELETE FROM chat_jobs WHERE updated_at<?
                   AND status NOT IN ('pending','processing','sending')""",
                (before,),
            ).rowcount
            connection.commit()
        return {"events": events, "jobs": jobs, "attempts": attempts}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


def _utc(value: datetime | None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
