"""Durable MAX dialog state, legal versions and event deduplication."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.database import Database, MAX_DIALOG_STATES
from app.domain import InvalidInputError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "new_user": {"legal_required"},
    "legal_required": {"main_menu"},
    "main_menu": {"waiting_for_source", "waiting_for_prompt", "gallery", "legal_required"},
    "waiting_for_source": {"waiting_for_prompt", "main_menu", "deleted"},
    "waiting_for_prompt": {"confirmation", "main_menu", "deleted"},
    "confirmation": {"processing", "waiting_for_prompt", "main_menu", "deleted"},
    "processing": {"result_ready", "demo_exhausted", "confirmation"},
    "result_ready": {
        "waiting_for_correction", "processing", "gallery", "main_menu",
        "demo_exhausted", "deleted",
    },
    "waiting_for_correction": {"confirmation", "result_ready", "deleted"},
    "demo_exhausted": {"gallery", "main_menu", "deleted"},
    "gallery": {
        "gallery", "result_ready", "waiting_for_correction", "processing",
        "main_menu", "deleted",
    },
    "deleted": {"main_menu", "waiting_for_source", "gallery"},
}


@dataclass(frozen=True)
class MaxDialog:
    platform_user_id: str
    chat_id: Optional[str]
    user_id: Optional[str]
    state: str
    selected_scenario_id: Optional[str]
    session_id: Optional[str]
    pending_prompt: Optional[str]
    pending_action: Optional[str]
    current_gallery_item_id: Optional[str]
    current_version_id: Optional[str]
    gallery_cursor: int
    status_message_id: Optional[str]


class MaxConversationStore:
    def __init__(
        self, database: Database, clock: Callable[[], datetime] = _now
    ) -> None:
        self.database = database
        self.clock = clock

    @staticmethod
    def _dialog(row: sqlite3.Row) -> MaxDialog:
        return MaxDialog(
            row["platform_user_id"], row["chat_id"], row["user_id"], row["state"],
            row["selected_scenario_id"], row["session_id"], row["pending_prompt"],
            row["pending_action"],
            row["current_gallery_item_id"], row["current_version_id"],
            row["gallery_cursor"], row["status_message_id"],
        )

    def get_or_create(self, platform_user_id: str, chat_id: Optional[str]) -> MaxDialog:
        now = _iso(self.clock())
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO max_dialogs(
                       platform_user_id,chat_id,state,created_at,updated_at
                   ) VALUES(?,?,'new_user',?,?)""",
                (platform_user_id, chat_id, now, now),
            )
            if chat_id is not None:
                connection.execute(
                    "UPDATE max_dialogs SET chat_id=?,updated_at=? WHERE platform_user_id=?",
                    (chat_id, now, platform_user_id),
                )
            row = connection.execute(
                "SELECT * FROM max_dialogs WHERE platform_user_id=?", (platform_user_id,)
            ).fetchone()
        return self._dialog(row)

    def get(self, platform_user_id: str) -> Optional[MaxDialog]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM max_dialogs WHERE platform_user_id=?", (platform_user_id,)
            ).fetchone()
        return self._dialog(row) if row else None

    def transition(
        self,
        platform_user_id: str,
        to_state: str,
        *,
        event_key: Optional[str] = None,
        force: bool = False,
        **fields: Any,
    ) -> MaxDialog:
        if to_state not in MAX_DIALOG_STATES:
            raise InvalidInputError("Unknown MAX dialog state")
        allowed_fields = {
            "chat_id", "user_id", "selected_scenario_id", "session_id", "pending_prompt",
            "pending_action",
            "current_gallery_item_id", "current_version_id", "gallery_cursor",
            "status_message_id",
        }
        if set(fields) - allowed_fields:
            raise InvalidInputError("Unknown MAX dialog field")
        now = _iso(self.clock())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM max_dialogs WHERE platform_user_id=?", (platform_user_id,)
            ).fetchone()
            if row is None:
                raise InvalidInputError("MAX dialog does not exist")
            if not force and to_state != row["state"] and to_state not in ALLOWED_TRANSITIONS[row["state"]]:
                raise InvalidInputError(
                    f"Invalid MAX dialog transition: {row['state']} -> {to_state}"
                )
            assignments = ["state=?", "updated_at=?"]
            parameters: list[Any] = [to_state, now]
            for name, value in fields.items():
                assignments.append(f"{name}=?")
                parameters.append(value)
            parameters.append(platform_user_id)
            connection.execute(
                f"UPDATE max_dialogs SET {','.join(assignments)} WHERE platform_user_id=?",
                parameters,
            )
            connection.execute(
                """INSERT INTO max_dialog_transitions(
                       platform_user_id,from_state,to_state,event_key,created_at
                   ) VALUES(?,?,?,?,?)""",
                (platform_user_id, row["state"], to_state, event_key, now),
            )
            updated = connection.execute(
                "SELECT * FROM max_dialogs WHERE platform_user_id=?", (platform_user_id,)
            ).fetchone()
        return self._dialog(updated)

    def update(self, platform_user_id: str, **fields: Any) -> MaxDialog:
        current = self.get(platform_user_id)
        if current is None:
            raise InvalidInputError("MAX dialog does not exist")
        return self.transition(platform_user_id, current.state, **fields)

    def begin_event(self, event_key: str, event_type: str) -> bool:
        now = _iso(self.clock())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM max_processed_events WHERE event_key=?", (event_key,)
            ).fetchone()
            if row and row["status"] in {"processing", "completed"}:
                return False
            if row:
                connection.execute(
                    """UPDATE max_processed_events SET status='processing',attempts=attempts+1,
                       updated_at=? WHERE event_key=?""",
                    (now, event_key),
                )
            else:
                connection.execute(
                    """INSERT INTO max_processed_events(
                           event_key,event_type,status,first_seen_at,updated_at
                       ) VALUES(?,?,'processing',?,?)""",
                    (event_key, event_type, now, now),
                )
        return True

    def finish_event(self, event_key: str, success: bool) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE max_processed_events SET status=?,updated_at=? WHERE event_key=?",
                ("completed" if success else "failed", _iso(self.clock()), event_key),
            )

    def required_documents(self) -> dict[str, str]:
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT document_type,version FROM max_legal_documents
                   WHERE required=1 AND active=1 ORDER BY document_type"""
            ).fetchall()
        return {row["document_type"]: row["version"] for row in rows}

    def legal_is_current(self, platform_user_id: str) -> bool:
        required = self.required_documents()
        if not required:
            return False
        with self.database.read() as connection:
            accepted = {
                row["document_type"]: row["document_version"]
                for row in connection.execute(
                    """SELECT document_type,document_version FROM max_legal_acceptances
                       WHERE platform_user_id=?""",
                    (platform_user_id,),
                ).fetchall()
            }
        return all(accepted.get(kind) == version for kind, version in required.items())

    def accept_required_documents(self, platform_user_id: str) -> None:
        now = _iso(self.clock())
        required = self.required_documents()
        with self.database.transaction() as connection:
            for document_type, version in required.items():
                connection.execute(
                    """INSERT OR IGNORE INTO max_legal_acceptances(
                           platform_user_id,document_type,document_version,accepted_at
                       ) VALUES(?,?,?,?)""",
                    (platform_user_id, document_type, version, now),
                )

    def get_marker(self) -> Optional[int]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT value FROM max_transport_state WHERE name='poll_marker'"
            ).fetchone()
        return int(row["value"]) if row and row["value"] is not None else None

    def set_marker(self, marker: Optional[int]) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO max_transport_state(name,value,updated_at)
                   VALUES('poll_marker',?,?)
                   ON CONFLICT(name) DO UPDATE SET value=excluded.value,
                       updated_at=excluded.updated_at""",
                (str(marker) if marker is not None else None, _iso(self.clock())),
            )

    def touch_poll_success(self) -> None:
        """Record a successful MAX response independently from marker movement."""

        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO max_transport_state(name,value,updated_at)
                   VALUES('poll_last_success','ok',?)
                   ON CONFLICT(name) DO UPDATE SET value='ok',updated_at=excluded.updated_at""",
                (_iso(self.clock()),),
            )

    def transport_state(self, name: str) -> Optional[tuple[Optional[str], str]]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT value,updated_at FROM max_transport_state WHERE name=?", (name,)
            ).fetchone()
        return (row["value"], row["updated_at"]) if row else None
