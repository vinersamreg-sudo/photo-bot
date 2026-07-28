"""Privacy-minimal product events for the closed Ravuna pilot."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
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
    "payment_clicked",
    "gallery_opened",
    "work_deleted",
    "work_restored",
    "favorite_changed",
    "current_best_changed",
    "demo_quota_exhausted",
    "payment_started",
    "payment_confirmed",
    "payment_failed",
    "original_delivered",
    "original_delivery_failed",
    "refund_started",
    "refund_confirmed",
    "refund_failed",
    "error",
    "initial_free_pack_granted",
    "generation_credit_reserved",
    "generation_credit_consumed",
    "generation_credit_released",
    "continuation_pack_clicked",
    "continuation_pack_payment_started",
    "continuation_pack_paid",
    "continuation_pack_failed",
    "unlock_entitlement_granted",
    "unlock_entitlement_consumed",
    "unlock_entitlement_unused",
    "repeat_pack_purchase",
    "generations_before_first_purchase",
    "packs_per_payer",
    "unlock_version_age",
    "balance_at_churn",
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
        subject_user_id: Optional[str] = None,
        value_integer: Optional[int] = None,
        value_real: Optional[float] = None,
    ) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError("Unknown telemetry event type")
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO product_events(
                       event_type,created_at,session_id,attempt_id,gallery_item_id,
                       error_type,duration_ms,estimated_cost,parser_fallback,
                       subject_hash,value_integer,value_real
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    (
                        hashlib.sha256(
                            f"pixora-product-subject:{subject_user_id}".encode()
                        ).hexdigest()
                        if subject_user_id else None
                    ),
                    value_integer,
                    value_real,
                ),
            )
