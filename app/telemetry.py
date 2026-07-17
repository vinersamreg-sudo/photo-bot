"""Privacy-minimal product events for the closed Pixora pilot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.database import Database


EVENT_TYPES = {
    "start",
    "photo_uploaded",
    "prompt_submitted",
    "processing_started",
    "result_delivered",
    "correction_started",
    "repeat_started",
    "feedback_positive",
    "feedback_negative",
    "unlock_clicked",
    "gallery_opened",
    "work_deleted",
    "error",
}


class TelemetryRecorder:
    """Records no prompt text, image bytes, platform IDs or biometric data."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def record(
        self,
        event_type: str,
        *,
        session_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        gallery_item_id: Optional[str] = None,
        error_type: Optional[str] = None,
        duration_ms: Optional[int] = None,
        estimated_cost: Optional[float] = None,
        parser_fallback: bool = False,
    ) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError("Unknown telemetry event type")
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO product_events(
                       event_type,created_at,session_id,attempt_id,gallery_item_id,
                       error_type,duration_ms,estimated_cost,parser_fallback
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    event_type,
                    datetime.now(timezone.utc).isoformat(),
                    session_id,
                    attempt_id,
                    gallery_item_id,
                    error_type,
                    duration_ms,
                    estimated_cost,
                    int(parser_fallback),
                ),
            )
