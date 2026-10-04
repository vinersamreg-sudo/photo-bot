"""Private, photo-optional service feedback without provider or media metadata."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from app.database import Database
from app.domain import InvalidInputError


MAX_FEEDBACK_MESSAGE_LENGTH = 4000
_SCREEN_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_key_hash(event_key: str) -> str:
    if not isinstance(event_key, str) or not event_key.strip():
        raise InvalidInputError("Feedback event key must not be empty")
    return hashlib.sha256(event_key.encode("utf-8")).hexdigest()


class FeedbackService:
    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.database = database
        self.clock = clock

    def has_event(self, user_id: str, event_key: str) -> bool:
        """Check a saved event without exposing another user's feedback."""
        key_hash = _event_key_hash(event_key)
        with self.database.read() as connection:
            return connection.execute(
                "SELECT 1 FROM service_feedback WHERE user_id=? AND event_key=?",
                (user_id, key_hash),
            ).fetchone() is not None

    def record_message(
        self,
        user_id: str,
        message: str,
        source_screen: str,
        *,
        event_key: str,
        gallery_item_id: str | None = None,
        version_id: str | None = None,
    ) -> str:
        """Preserve exact user text; hash transport keys and reject conflicting replay.

        Screen names are technical tokens, with the product screen allowlist owned
        by the caller. Optional context must belong to this user. Replaying a saved
        event matches its user and exact text even after gallery retention clears
        the old context; it never changes the original timestamp or screen.
        """
        if not isinstance(message, str) or not message.strip():
            raise InvalidInputError("Feedback message must not be empty")
        if len(message) > MAX_FEEDBACK_MESSAGE_LENGTH:
            raise InvalidInputError(
                f"Feedback message must not exceed {MAX_FEEDBACK_MESSAGE_LENGTH} characters"
            )
        if not isinstance(source_screen, str) or not _SCREEN_TOKEN.fullmatch(source_screen):
            raise InvalidInputError("Invalid feedback source screen")
        key_hash = _event_key_hash(event_key)

        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id,user_id,message FROM service_feedback WHERE event_key=?",
                (key_hash,),
            ).fetchone()
            if existing is not None:
                if existing["user_id"] != user_id or existing["message"] != message:
                    raise InvalidInputError("Feedback event key conflicts with saved feedback")
                return str(existing["id"])

            if connection.execute(
                "SELECT 1 FROM users WHERE id=?", (user_id,)
            ).fetchone() is None:
                raise InvalidInputError("Unknown feedback user")

            if gallery_item_id is not None and connection.execute(
                "SELECT 1 FROM gallery_items WHERE id=? AND user_id=?",
                (gallery_item_id, user_id),
            ).fetchone() is None:
                raise InvalidInputError("Unknown feedback gallery work")

            if version_id is not None:
                version = connection.execute(
                    """SELECT v.gallery_item_id FROM gallery_versions AS v
                       JOIN gallery_items AS g ON g.id=v.gallery_item_id
                       WHERE v.id=? AND g.user_id=?""",
                    (version_id, user_id),
                ).fetchone()
                if version is None:
                    raise InvalidInputError("Unknown feedback gallery version")
                if gallery_item_id is not None and gallery_item_id != version["gallery_item_id"]:
                    raise InvalidInputError("Feedback gallery context does not match")
                gallery_item_id = str(version["gallery_item_id"])

            feedback_id = uuid4().hex
            connection.execute(
                """INSERT INTO service_feedback(
                       id,user_id,message,created_at,source_screen,
                       gallery_item_id,version_id,event_key
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    feedback_id, user_id, message,
                    self.clock().astimezone(timezone.utc).isoformat(), source_screen,
                    gallery_item_id, version_id, key_hash,
                ),
            )
            return feedback_id
