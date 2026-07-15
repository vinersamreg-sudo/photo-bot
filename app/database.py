"""SQLite persistence and transactional guards for demo operations."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    demo_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    risk_score INTEGER NOT NULL DEFAULT 0,
    blocked_until TEXT,
    UNIQUE(platform, platform_user_id)
);
CREATE TABLE IF NOT EXISTS demo_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    source_file_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    successful_generations INTEGER NOT NULL DEFAULT 0,
    max_generations INTEGER NOT NULL,
    completed_at TEXT,
    converted_to_paid INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id)
);
CREATE TABLE IF NOT EXISTS generation_attempts (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES demo_sessions(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    prompt TEXT NOT NULL,
    scenario_id TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    source_path TEXT NOT NULL,
    original_result_path TEXT,
    demo_result_path TEXT,
    error_type TEXT,
    error_message_safe TEXT,
    estimated_cost REAL,
    external_request_id TEXT,
    duration_ms INTEGER,
    input_size_bytes INTEGER,
    output_size_bytes INTEGER,
    requested_size TEXT,
    requested_quality TEXT,
    output_format TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    technical_refund INTEGER NOT NULL DEFAULT 0,
    correction INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT,
    result_unlocked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_user_started ON generation_attempts(user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON generation_attempts(status);
CREATE TABLE IF NOT EXISTS payment_intents (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES generation_attempts(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    amount_rub INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(attempt_id)
);
CREATE TABLE IF NOT EXISTS legal_consents (
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    offer_accepted INTEGER NOT NULL,
    personal_data_accepted INTEGER NOT NULL,
    image_rights_confirmed INTEGER NOT NULL,
    external_ai_acknowledged INTEGER NOT NULL,
    appearance_change_acknowledged INTEGER NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY(platform, platform_user_id)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(SCHEMA)
            existing = {
                row[1] for row in connection.execute("PRAGMA table_info(generation_attempts)")
            }
            for name, declaration in (
                ("requested_size", "TEXT"),
                ("requested_quality", "TEXT"),
                ("output_format", "TEXT"),
            ):
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE generation_attempts ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                """UPDATE generation_attempts
                   SET status='failed_technical', completed_at=datetime('now'),
                       error_type='process_restarted',
                       error_message_safe='Generation interrupted by process restart',
                       technical_refund=1
                   WHERE status IN ('pending', 'processing')"""
            )
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
