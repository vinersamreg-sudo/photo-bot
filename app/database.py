"""SQLite persistence and transactional guards for demo operations."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import NAMESPACE_URL, uuid5


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


GALLERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS galleries (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS gallery_items (
    id TEXT PRIMARY KEY,
    gallery_id TEXT NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scenario_id TEXT,
    original_source_path TEXT NOT NULL,
    storage_root_path TEXT NOT NULL,
    current_best_version_id TEXT,
    favorite INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    purge_after TEXT,
    purged_at TEXT,
    cover_preview_path TEXT,
    preview_small_path TEXT,
    preview_large_path TEXT,
    generation_count INTEGER NOT NULL DEFAULT 0,
    folder_id TEXT REFERENCES collections(id) ON DELETE SET NULL,
    last_opened_at TEXT,
    unlock_status TEXT NOT NULL DEFAULT 'demo',
    retention_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gallery_user_updated ON gallery_items(user_id, deleted, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_gallery_search ON gallery_items(user_id, title, scenario_id, folder_id, favorite, deleted);
CREATE TABLE IF NOT EXISTS gallery_versions (
    id TEXT PRIMARY KEY,
    gallery_item_id TEXT NOT NULL REFERENCES gallery_items(id) ON DELETE CASCADE,
    attempt_id TEXT UNIQUE REFERENCES generation_attempts(id) ON DELETE SET NULL,
    version_number INTEGER NOT NULL,
    parent_version_id TEXT REFERENCES gallery_versions(id) ON DELETE SET NULL,
    source_path TEXT NOT NULL,
    prompt TEXT NOT NULL,
    correction_prompt TEXT,
    effective_prompt TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    preview_watermarked_path TEXT,
    original_path TEXT,
    created_at TEXT NOT NULL,
    processing_time_ms INTEGER,
    estimated_cost REAL,
    status TEXT NOT NULL,
    rating INTEGER,
    favorite INTEGER NOT NULL DEFAULT 0,
    unlock_status TEXT NOT NULL DEFAULT 'demo',
    UNIQUE(gallery_item_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_versions_item_number ON gallery_versions(gallery_item_id, version_number DESC);
CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, normalized_name)
);
CREATE TABLE IF NOT EXISTS gallery_item_tags (
    gallery_item_id TEXT NOT NULL REFERENCES gallery_items(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY(gallery_item_id, tag_id)
);
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    favorite_style TEXT,
    favorite_background TEXT,
    favorite_clothing TEXT,
    favorite_format TEXT,
    favorite_quality TEXT,
    favorite_scenarios_json TEXT NOT NULL DEFAULT '[]',
    recent_scenarios_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
"""


MAX_DIALOG_STATES = (
    "new_user", "legal_required", "main_menu", "waiting_for_source",
    "waiting_for_prompt", "confirmation", "processing", "result_ready",
    "waiting_for_correction", "demo_exhausted", "gallery", "deleted",
)

MAX_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS max_dialogs (
    platform_user_id TEXT PRIMARY KEY,
    chat_id TEXT,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK(state IN ({','.join(repr(value) for value in MAX_DIALOG_STATES)})),
    selected_scenario_id TEXT,
    session_id TEXT REFERENCES demo_sessions(id) ON DELETE SET NULL,
    pending_prompt TEXT,
    current_gallery_item_id TEXT REFERENCES gallery_items(id) ON DELETE SET NULL,
    current_version_id TEXT REFERENCES gallery_versions(id) ON DELETE SET NULL,
    gallery_cursor INTEGER NOT NULL DEFAULT 0,
    status_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS max_legal_documents (
    document_type TEXT NOT NULL,
    version TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    draft INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY(document_type, version)
);
CREATE TABLE IF NOT EXISTS max_legal_acceptances (
    platform_user_id TEXT NOT NULL,
    document_type TEXT NOT NULL,
    document_version TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY(platform_user_id, document_type, document_version),
    FOREIGN KEY(document_type, document_version)
        REFERENCES max_legal_documents(document_type, version)
);
CREATE TABLE IF NOT EXISTS max_processed_events (
    event_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('processing','completed','failed')),
    attempts INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_max_events_status ON max_processed_events(status, updated_at);
CREATE TABLE IF NOT EXISTS max_transport_state (
    name TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS max_dialog_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_user_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    event_key TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_max_transitions_user ON max_dialog_transitions(platform_user_id, id DESC);
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
                ("parent_version_id", "TEXT"),
                ("correction_prompt", "TEXT"),
                ("effective_prompt", "TEXT"),
            ):
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE generation_attempts ADD COLUMN {name} {declaration}"
                    )
            session_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(demo_sessions)")
            }
            if "gallery_item_id" not in session_columns:
                connection.execute(
                    "ALTER TABLE demo_sessions ADD COLUMN gallery_item_id TEXT"
                )
            connection.executescript(GALLERY_SCHEMA)
            migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=2"
            ).fetchone()
            if migration is None:
                self._backfill_gallery(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(2,?,?)",
                    ("personal_ai_studio_gallery", datetime.now(timezone.utc).isoformat()),
                )
            connection.executescript(MAX_SCHEMA)
            max_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=3"
            ).fetchone()
            if max_migration is None:
                now = datetime.now(timezone.utc).isoformat()
                for document_type in ("offer", "personal_data", "image_rights", "external_ai"):
                    connection.execute(
                        """INSERT OR IGNORE INTO max_legal_documents(
                               document_type,version,required,draft,active,created_at
                           ) VALUES(?,?,1,1,1,?)""",
                        (document_type, "2026-07-draft-1", now),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(3,?,?)",
                    ("max_dialog_state_and_legal_versions", now),
                )
            connection.execute(
                """UPDATE max_dialogs SET state='confirmation',status_message_id=NULL,
                   updated_at=datetime('now') WHERE state='processing'"""
            )
            connection.execute(
                """UPDATE max_processed_events SET status='failed',updated_at=datetime('now')
                   WHERE status='processing'"""
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

    @staticmethod
    def _backfill_gallery(connection: sqlite3.Connection) -> None:
        sessions = connection.execute(
            "SELECT * FROM demo_sessions WHERE gallery_item_id IS NULL"
        ).fetchall()
        for session in sessions:
            gallery_id = uuid5(
                NAMESPACE_URL, f"photo-bot:user-gallery:{session['user_id']}"
            ).hex
            connection.execute(
                """INSERT OR IGNORE INTO galleries(id,user_id,created_at,updated_at)
                   VALUES(?,?,?,?)""",
                (gallery_id, session["user_id"], session["created_at"], session["updated_at"]),
            )
            item_id = uuid5(NAMESPACE_URL, f"photo-bot:gallery:{session['id']}").hex
            root = str(Path(session["source_file_path"]).parent.parent)
            retention = (
                datetime.fromisoformat(session["started_at"])
                + timedelta(days=180 if session["converted_to_paid"] else 30)
            ).isoformat()
            attempts = connection.execute(
                """SELECT * FROM generation_attempts
                   WHERE session_id=? AND status='succeeded'
                   ORDER BY completed_at, created_at""",
                (session["id"],),
            ).fetchall()
            unlock_status = "unlocked" if any(row["result_unlocked"] for row in attempts) else "demo"
            connection.execute(
                """INSERT OR IGNORE INTO gallery_items(
                       id,gallery_id,user_id,title,created_at,updated_at,scenario_id,original_source_path,
                       storage_root_path,generation_count,last_opened_at,unlock_status,retention_until
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id,
                    gallery_id,
                    session["user_id"],
                    "Моя работа",
                    session["created_at"],
                    session["updated_at"],
                    attempts[-1]["scenario_id"] if attempts else None,
                    session["source_file_path"],
                    root,
                    len(attempts),
                    session["updated_at"],
                    unlock_status,
                    retention,
                ),
            )
            last_version_id = None
            for number, attempt in enumerate(attempts, start=1):
                version_id = uuid5(
                    NAMESPACE_URL, f"photo-bot:gallery-version:{attempt['id']}"
                ).hex
                effective_prompt = attempt["effective_prompt"] or attempt["prompt"]
                version_unlock = "unlocked" if attempt["result_unlocked"] else "demo"
                connection.execute(
                    """INSERT OR IGNORE INTO gallery_versions(
                           id,gallery_item_id,attempt_id,version_number,parent_version_id,
                           source_path,prompt,correction_prompt,effective_prompt,provider,model,
                           preview_watermarked_path,original_path,created_at,processing_time_ms,
                           estimated_cost,status,unlock_status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        version_id,
                        item_id,
                        attempt["id"],
                        number,
                        attempt["parent_version_id"],
                        attempt["source_path"],
                        attempt["prompt"],
                        attempt["correction_prompt"],
                        effective_prompt,
                        attempt["provider"],
                        attempt["model"],
                        attempt["demo_result_path"],
                        attempt["original_result_path"],
                        attempt["completed_at"] or attempt["created_at"],
                        attempt["duration_ms"],
                        attempt["estimated_cost"],
                        attempt["status"],
                        version_unlock,
                    ),
                )
                last_version_id = version_id
            if last_version_id:
                last = attempts[-1]
                connection.execute(
                    """UPDATE gallery_items SET current_best_version_id=?,cover_preview_path=?,
                       preview_small_path=?,preview_large_path=? WHERE id=?""",
                    (
                        last_version_id,
                        last["demo_result_path"],
                        last["demo_result_path"],
                        last["demo_result_path"],
                        item_id,
                    ),
                )
            connection.execute(
                "UPDATE demo_sessions SET gallery_item_id=? WHERE id=?",
                (item_id, session["id"]),
            )

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
