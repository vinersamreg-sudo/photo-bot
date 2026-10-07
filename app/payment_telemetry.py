"""Bounded payment journey metadata, independent of payment confirmation.

Uses the existing private payment_audit table: no schema migration, URLs,
tokens, request payloads, bank data or customer contact information.
Browser hits are observations, never proof of payment or provider attempts.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
import sqlite3

LOGGER = logging.getLogger(__name__)
EVENTS = frozenset({
    "checkout_created",
    "payment_link_opened", "payment_link_reopened", "success_url_return",
    "fail_url_return", "max_payfail_return", "payment_status_checked",
    "checkout_created_after_failure",
})
FIRST_ONLY = frozenset({"checkout_created", "success_url_return", "fail_url_return", "checkout_created_after_failure"})


def record(database, order_id: str, event_type: str, *, at: datetime,
           event_key: str | None = None, related_order_id: str | None = None) -> bool:
    """Best effort only; a diagnostics failure cannot block checkout/return.

    BEGIN IMMEDIATE serializes first-hit/reopen and MAX replay deduplication.
    Browser returns are first-only because SuccessURL auto-refreshes while
    waiting for ResultURL; refreshes are not additional payment attempts.
    """
    if event_type not in EVENTS:
        raise ValueError("Unknown payment journey event")
    metadata = {"telemetry_version": 1}
    if event_key is not None:
        metadata["event_hash"] = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
    try:
        with database.transaction() as connection:
            order = connection.execute("SELECT status FROM payment_orders WHERE id=?", (order_id,)).fetchone()
            if order is None:
                return False
            if related_order_id is not None:
                # Only a same-owner, same-product, same-target previous order
                # with a recorded failure return can be linked. IDs remain
                # private foreign keys and are never included in reports.
                previous = connection.execute("""
                    SELECT p.id FROM payment_orders p JOIN payment_orders n ON n.id=?
                    WHERE p.id=? AND p.user_id=n.user_id AND p.product_code=n.product_code
                      AND p.payment_purpose=n.payment_purpose
                      AND COALESCE(p.version_id,'')=COALESCE(n.version_id,'')
                      AND p.pending_request_id IS n.pending_request_id
                      AND p.created_at<n.created_at AND p.paid_at IS NULL
                      AND EXISTS(SELECT 1 FROM payment_audit a WHERE a.order_id=p.id
                        AND a.event_type IN ('fail_url_return','max_payfail_return'))
                """, (order_id, related_order_id)).fetchone()
                if previous is None:
                    return False
                metadata["previous_order_id"] = related_order_id
            reason = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
            if event_type in FIRST_ONLY:
                duplicate = connection.execute(
                    "SELECT 1 FROM payment_audit WHERE order_id=? AND event_type=? LIMIT 1",
                    (order_id, event_type),
                ).fetchone()
            elif event_key is not None:
                duplicate = connection.execute(
                    "SELECT 1 FROM payment_audit WHERE order_id=? AND event_type=? AND reason=? LIMIT 1",
                    (order_id, event_type, reason),
                ).fetchone()
            else:
                duplicate = None
            if duplicate:
                return False
            if event_type == "payment_link_opened" and connection.execute(
                "SELECT 1 FROM payment_audit WHERE order_id=? AND event_type='payment_link_opened' LIMIT 1",
                (order_id,),
            ).fetchone():
                event_type = "payment_link_reopened"
            connection.execute("""INSERT INTO payment_audit(
                order_id,event_type,from_status,to_status,reason,actor_type,created_at
                ) VALUES(?,?,?,?,?,'journey',?)""",
                (order_id, event_type, order["status"], order["status"], reason, at.isoformat()),
            )
        return True
    except (sqlite3.Error, OSError):
        LOGGER.warning("Payment journey telemetry unavailable (event_type=%s)", event_type)
        return False
