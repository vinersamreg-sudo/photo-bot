"""Privacy-safe, fail-closed operator views for payments and the closed pilot."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from app.config import Settings
from app.database import Database
from app.commerce import CONTINUATION_PACKAGES, is_continuation_package
from app.payments import PaymentError, PaymentStatus


PAID_STATES = {
    PaymentStatus.PAID.value,
    PaymentStatus.DELIVERY_PENDING.value,
    PaymentStatus.DELIVERED.value,
    PaymentStatus.REFUND_PENDING.value,
    PaymentStatus.PARTIALLY_REFUNDED.value,
}
ECONOMICALLY_PAID_STATES = (*sorted(PAID_STATES), PaymentStatus.REFUNDED.value)


def mask_reference(value: object, *, prefix: str = "ref") -> str | None:
    """Return a stable-enough operator hint without disclosing the full value."""

    if value is None:
        return None
    text = str(value)
    tail = text[-4:] if len(text) > 4 else text
    return f"{prefix}-…{tail}"


def _order_row(database: Database, invoice: int):
    if invoice <= 0:
        raise PaymentError("Invoice must be a positive integer")
    with database.read() as connection:
        row = connection.execute(
            """SELECT o.*,v.unlock_status,v.original_path,v.delivery_count,
                      v.attempt_id AS version_attempt_id,i.user_id AS item_user_id,
                      a.user_id AS attempt_user_id,
                      r.amount_minor AS receipt_amount_minor,r.status AS receipt_status,
                      g.status AS grant_status,l.available_credits,l.reserved_credits,
                      l.consumed_credits,e.status AS entitlement_status,
                      (SELECT COUNT(*) FROM unlock_entitlements owned
                       WHERE owned.source_payment_order_id=o.id) AS entitlement_count,
                      (SELECT COUNT(*) FROM unlock_entitlements owned
                       WHERE owned.source_payment_order_id=o.id
                         AND owned.status IN ('available','reserved','consumed'))
                         AS active_entitlement_count,
                      (SELECT COUNT(*) FROM unlock_entitlements owned
                       WHERE owned.source_payment_order_id=o.id
                         AND owned.status='refunded') AS refunded_entitlement_count
               FROM payment_orders o
               LEFT JOIN gallery_versions v ON v.id=o.version_id
               LEFT JOIN gallery_items i ON i.id=v.gallery_item_id
               LEFT JOIN generation_attempts a ON a.id=o.attempt_id
               LEFT JOIN payment_receipts r ON r.order_id=o.id AND r.receipt_type='payment'
               LEFT JOIN continuation_pack_grants g ON g.payment_order_id=o.id
               LEFT JOIN generation_credit_lots l ON l.id=g.credit_lot_id
               LEFT JOIN unlock_entitlements e ON e.id=g.entitlement_id
               WHERE o.provider_invoice_id=?""",
            (invoice,),
        ).fetchone()
    if row is None:
        raise PaymentError("Payment order was not found")
    return row


def payment_show(database: Database, invoice: int) -> dict[str, Any]:
    row = _order_row(database, invoice)
    with database.read() as connection:
        audit = connection.execute(
            """SELECT event_type,from_status,to_status,reason,actor_type,created_at
               FROM payment_audit WHERE order_id=? ORDER BY id""",
            (row["id"],),
        ).fetchall()
        refunds = connection.execute(
            """SELECT status,COUNT(*) AS count,COALESCE(SUM(amount_minor),0) AS amount_minor
               FROM refund_intents WHERE order_id=? GROUP BY status""",
            (row["id"],),
        ).fetchall()
    status = str(row["status"])
    original_available = bool(row["original_path"] and Path(row["original_path"]).is_file())
    if row["payment_purpose"] == "account_topup":
        ownership_consistent = bool(
            row["user_id"] and not row["version_id"] and not row["attempt_id"]
        )
        exact_version_consistent = True
    else:
        ownership_consistent = bool(
            row["user_id"]
            and row["user_id"] == row["item_user_id"] == row["attempt_user_id"]
        )
        exact_version_consistent = bool(
            row["version_attempt_id"]
            and row["version_attempt_id"] == row["attempt_id"]
        )
    receipt_consistent = bool(
        row["receipt_amount_minor"] is not None
        and int(row["receipt_amount_minor"]) == int(row["amount_minor"])
        and row["currency"] == "RUB"
    )
    is_pack = is_continuation_package(str(row["product_code"]))
    if is_pack and status in PAID_STATES:
        unlock_consistent = bool(
            row["grant_status"] == "active"
            and int(row["entitlement_count"] or 0)
            == int(row["unlock_entitlement_quantity"] or 0)
            and int(row["active_entitlement_count"] or 0)
            == int(row["unlock_entitlement_quantity"] or 0)
            and int(row["available_credits"] or 0)
            + int(row["reserved_credits"] or 0)
            + int(row["consumed_credits"] or 0)
            == int(row["generation_credit_quantity"] or 0)
        )
    elif is_pack and status == PaymentStatus.REFUNDED.value:
        unlock_consistent = bool(
            row["grant_status"] == "refunded"
            and int(row["entitlement_count"] or 0)
            == int(row["unlock_entitlement_quantity"] or 0)
            and int(row["refunded_entitlement_count"] or 0)
            == int(row["unlock_entitlement_quantity"] or 0)
        )
    elif status in PAID_STATES:
        unlock_consistent = row["unlock_status"] == "unlocked"
    elif status == PaymentStatus.REFUNDED.value:
        unlock_consistent = row["unlock_status"] == "refunded"
    else:
        unlock_consistent = row["unlock_status"] in {None, "demo"}
    checks = {
        "ownership_consistent": ownership_consistent,
        "exact_version_consistent": exact_version_consistent,
        "receipt_consistent": receipt_consistent,
        "unlock_consistent": bool(unlock_consistent),
        "audit_present": bool(audit),
    }
    if not is_pack:
        checks["original_available"] = original_available
    return {
        "invoice_ref": mask_reference(invoice, prefix="inv"),
        "order_ref": mask_reference(row["id"], prefix="ord"),
        "version_ref": mask_reference(row["version_id"], prefix="ver"),
        "status": status,
        "amount_rub": int(row["amount_minor"]) / 100,
        "currency": str(row["currency"]),
        "provider": str(row["provider"]),
        "product_code": str(row["product_code"]),
        "created_at": str(row["created_at"]),
        "expires_at": str(row["expires_at"]),
        "paid_at": row["paid_at"],
        "delivered_at": row["delivered_at"],
        "failure_code": row["failure_code"],
        "delivery_count": int(row["delivery_count"] or 0),
        "package": {
            "grant_status": row["grant_status"],
            "variants_available": row["available_credits"],
            "variants_reserved": row["reserved_credits"],
            "variants_consumed": row["consumed_credits"],
            "original_status": row["entitlement_status"],
            "original_quantity": row["entitlement_count"],
        } if is_pack else None,
        "receipt_status": row["receipt_status"],
        "refunded_rub": int(row["refunded_amount_minor"] or 0) / 100,
        "refunds": {
            str(item["status"]): {
                "count": int(item["count"]),
                "amount_rub": int(item["amount_minor"]) / 100,
            }
            for item in refunds
        },
        "checks": checks,
        "consistent": all(checks.values()),
        "audit": [
            {
                "event_type": str(item["event_type"]),
                "from_status": item["from_status"],
                "to_status": item["to_status"],
                "reason": item["reason"],
                "actor_type": str(item["actor_type"]),
                "created_at": str(item["created_at"]),
            }
            for item in audit
        ],
    }


def payment_reconcile(database: Database, invoice: int | None = None) -> dict[str, Any]:
    if invoice is not None:
        order = payment_show(database, invoice)
        return {
            "scope": "single_invoice",
            "checked": 1,
            "mismatch_count": 0 if order["consistent"] else 1,
            "orders": [order],
        }
    with database.read() as connection:
        invoices = [
            int(row[0])
            for row in connection.execute(
                "SELECT provider_invoice_id FROM payment_orders ORDER BY provider_invoice_id"
            ).fetchall()
        ]
    orders = [payment_show(database, value) for value in invoices]
    return {
        "scope": "all_local_orders",
        "checked": len(orders),
        "mismatch_count": sum(1 for order in orders if not order["consistent"]),
        "orders": orders,
    }


def payment_reconciliation_summary(database: Database) -> dict[str, Any]:
    """Return an aggregate, read-only payment consistency report without PII."""

    paid_placeholders = ",".join("?" for _ in ECONOMICALLY_PAID_STATES)
    with database.read() as connection:
        orders_by_status = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                "SELECT status,COUNT(*) AS count FROM payment_orders GROUP BY status"
            ).fetchall()
        }
        paid_orders = int(connection.execute(
            f"SELECT COUNT(*) FROM payment_orders WHERE status IN ({paid_placeholders})",
            ECONOMICALLY_PAID_STATES,
        ).fetchone()[0])
        package_grants = int(connection.execute(
            "SELECT COUNT(*) FROM continuation_pack_grants"
        ).fetchone()[0])
        original_entitlements = int(connection.execute(
            "SELECT COUNT(*) FROM unlock_entitlements WHERE source_payment_order_id IS NOT NULL"
        ).fetchone()[0])
        receipts = int(connection.execute(
            "SELECT COUNT(*) FROM payment_receipts WHERE receipt_type='payment'"
        ).fetchone()[0])
        duplicate_callbacks = int(connection.execute(
            """SELECT COUNT(*) FROM payment_audit
               WHERE event_type='duplicate_callback_accepted'"""
        ).fetchone()[0])
        rejected_callbacks = int(connection.execute(
            "SELECT COUNT(*) FROM payment_events WHERE status='rejected'"
        ).fetchone()[0])
        rejected_callbacks_recent = int(connection.execute(
            """SELECT COUNT(*) FROM payment_events
               WHERE status='rejected'
                 AND datetime(received_at)>=datetime('now','-24 hours')"""
        ).fetchone()[0])
        expired_unpaid_orders = int(connection.execute(
            """SELECT COUNT(*) FROM payment_orders
               WHERE status='pending' AND datetime(expires_at)<=datetime('now')"""
        ).fetchone()[0])
        expired_checkout_events = int(connection.execute(
            """SELECT COUNT(*) FROM payment_audit
               WHERE event_type='checkout_expired'"""
        ).fetchone()[0])
        refreshed_checkout_events = int(connection.execute(
            """SELECT COUNT(*) FROM payment_audit
               WHERE event_type='checkout_refreshed'"""
        ).fetchone()[0])
        package_codes = tuple(CONTINUATION_PACKAGES)
        package_placeholders = ",".join("?" for _ in package_codes)
        paid_without_grant = int(connection.execute(
            f"""SELECT COUNT(*) FROM payment_orders o
                WHERE o.product_code IN ({package_placeholders})
                  AND o.status IN ({paid_placeholders})
                  AND NOT EXISTS(
                      SELECT 1 FROM continuation_pack_grants g
                      WHERE g.payment_order_id=o.id
                  )""",
            (*package_codes, *ECONOMICALLY_PAID_STATES),
        ).fetchone()[0])
        grant_without_paid_order = int(connection.execute(
            f"""SELECT COUNT(*) FROM continuation_pack_grants g
                LEFT JOIN payment_orders o ON o.id=g.payment_order_id
                WHERE o.id IS NULL OR o.status NOT IN ({paid_placeholders})""",
            ECONOMICALLY_PAID_STATES,
        ).fetchone()[0])
        original_entitlement_mismatches = int(connection.execute(
            """SELECT COUNT(*) FROM continuation_pack_grants g
               LEFT JOIN payment_orders o ON o.id=g.payment_order_id
               WHERE (SELECT COUNT(*) FROM unlock_entitlements e
                      WHERE e.source_payment_order_id=g.payment_order_id)
                        <>g.unlock_entitlement_quantity
                  OR EXISTS(
                      SELECT 1 FROM unlock_entitlements e
                      WHERE e.source_payment_order_id=g.payment_order_id
                        AND e.user_id<>g.user_id
                  )
                  OR o.user_id<>g.user_id
                  OR g.unlock_entitlement_quantity<=0"""
        ).fetchone()[0])
        receipt_mismatches = int(connection.execute(
            f"""SELECT COUNT(*) FROM payment_orders o
                LEFT JOIN payment_receipts r
                  ON r.order_id=o.id AND r.receipt_type='payment'
                WHERE o.status IN ({paid_placeholders})
                  AND (r.id IS NULL OR r.amount_minor<>o.amount_minor)""",
            ECONOMICALLY_PAID_STATES,
        ).fetchone()[0])
        duplicate_effects = int(connection.execute(
            """SELECT COALESCE(SUM(extra),0) FROM (
                   SELECT COUNT(*)-1 AS extra FROM continuation_pack_grants
                   GROUP BY payment_order_id HAVING COUNT(*)>1
                   UNION ALL
                   SELECT COUNT(*)-g.unlock_entitlement_quantity AS extra
                   FROM continuation_pack_grants g
                   JOIN unlock_entitlements e
                     ON e.source_payment_order_id=g.payment_order_id
                   GROUP BY g.id
                   HAVING COUNT(*)>g.unlock_entitlement_quantity
                   UNION ALL
                   SELECT COUNT(*)-1 AS extra FROM payment_receipts
                   WHERE receipt_type='payment'
                   GROUP BY order_id HAVING COUNT(*)>1
                   UNION ALL
                   SELECT COUNT(*)-1 AS extra FROM generation_credit_lots
                   WHERE source_payment_order_id IS NOT NULL
                   GROUP BY source_payment_order_id HAVING COUNT(*)>1
               )"""
        ).fetchone()[0])
        failed_original_delivery = int(connection.execute(
            "SELECT COUNT(*) FROM payment_orders WHERE status='delivery_pending'"
        ).fetchone()[0])
        delayed_callbacks = int(connection.execute(
            """SELECT COUNT(*) FROM payment_orders
               WHERE paid_at IS NOT NULL
                 AND datetime(paid_at)>datetime(expires_at)"""
        ).fetchone()[0])

    hard_failures = (
        paid_without_grant
        + grant_without_paid_order
        + original_entitlement_mismatches
        + receipt_mismatches
        + duplicate_effects
    )
    warnings = failed_original_delivery + rejected_callbacks_recent
    return {
        "scope": "aggregate_local_ledger",
        "payment_orders_by_status": orders_by_status,
        "paid_orders": paid_orders,
        "package_grants": package_grants,
        "original_entitlements": original_entitlements,
        "receipts": receipts,
        "duplicate_callback_evidence": duplicate_callbacks,
        "rejected_callbacks": rejected_callbacks,
        "rejected_callbacks_recent": rejected_callbacks_recent,
        "expired_unpaid_orders": expired_unpaid_orders,
        "expired_checkout_events": expired_checkout_events,
        "refreshed_checkout_events": refreshed_checkout_events,
        "paid_without_grant": paid_without_grant,
        "grant_without_paid_order": grant_without_paid_order,
        "original_entitlement_mismatches": original_entitlement_mismatches,
        "receipt_mismatches": receipt_mismatches,
        "duplicate_effects": duplicate_effects,
        "failed_original_delivery": failed_original_delivery,
        "delayed_callback_evidence": delayed_callbacks,
        "status": "CRITICAL" if hard_failures else "WARNING" if warnings else "OK",
        "read_only": True,
    }


def payment_expiration_reconcile(
    database: Database,
    *,
    apply: bool = False,
    clock=lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Expire only pending intents whose linked order is already final-expired."""

    with database.read() as connection:
        candidates = connection.execute(
            """SELECT i.id AS intent_id,o.id AS order_id
               FROM payment_intents i
               JOIN payment_orders o ON o.intent_id=i.id
               WHERE i.status='pending' AND o.status='expired'
               ORDER BY i.created_at,i.id"""
        ).fetchall()
    applied = 0
    if apply and candidates:
        now = clock().astimezone(timezone.utc).isoformat()
        with database.transaction() as connection:
            for candidate in candidates:
                result = connection.execute(
                    """UPDATE payment_intents
                       SET status='expired',updated_at=?
                       WHERE id=? AND status='pending'
                         AND EXISTS(
                             SELECT 1 FROM payment_orders o
                             WHERE o.intent_id=payment_intents.id
                               AND o.id=? AND o.status='expired'
                         )""",
                    (now, candidate["intent_id"], candidate["order_id"]),
                )
                if result.rowcount != 1:
                    continue
                connection.execute(
                    """INSERT INTO payment_audit(
                           order_id,event_type,from_status,to_status,reason,
                           actor_type,actor_ref_hash,created_at
                       )
                       SELECT ?,?,?,?,?,?,?,?
                       WHERE NOT EXISTS(
                           SELECT 1 FROM payment_audit
                           WHERE order_id=?
                             AND event_type='payment_intent_expiration_reconciled'
                       )""",
                    (
                        candidate["order_id"],
                        "payment_intent_expiration_reconciled",
                        "pending",
                        "expired",
                        "linked_order_expired",
                        "operator",
                        None,
                        now,
                        candidate["order_id"],
                    ),
                )
                applied += 1
    return {
        "mode": "apply" if apply else "dry-run",
        "candidate_count": len(candidates),
        "applied_count": applied,
        "database_mutated": bool(apply and applied),
        "intents": [
            {
                "intent_ref": mask_reference(row["intent_id"], prefix="int"),
                "order_ref": mask_reference(row["order_id"], prefix="ord"),
                "from_status": "pending",
                "to_status": "expired",
            }
            for row in candidates
        ],
    }


def robokassa_health(settings: Settings, database: Database) -> dict[str, Any]:
    result = urlsplit(settings.payment_result_url)
    success = urlsplit(settings.payment_success_url)
    failure = urlsplit(settings.payment_fail_url)
    result_expected = f"https://pixoraai.ru{settings.payment_webhook_path}"
    success_expected = "https://ravuna.ru/payment-success.html"
    failure_expected = "https://ravuna.ru/payment-failed.html"
    checks = {
        "provider_selected": settings.payment_provider == "robokassa",
        "sandbox_mode": settings.robokassa_mode == "sandbox",
        "production_not_approved": not settings.robokassa_production_approved,
        "credentials_present": bool(
            settings.robokassa_merchant_login
            and settings.robokassa_password1
            and settings.robokassa_password2
        ),
        "refund_password_present": bool(settings.robokassa_password3),
        "result_url_https": result.scheme == "https",
        "result_url_exact": settings.payment_result_url == result_expected,
        "success_url_https": success.scheme == "https",
        "fail_url_https": failure.scheme == "https",
        "success_url_exact": settings.payment_success_url == success_expected,
        "fail_url_exact": settings.payment_fail_url == failure_expected,
        "return_urls_without_query": not success.query and not failure.query,
        "listener_loopback": settings.payment_webhook_host in {"127.0.0.1", "::1", "localhost"},
        "webhook_path_exact": settings.payment_webhook_path == "/payments/robokassa/result",
        "currency_rub": settings.payment_currency == "RUB",
        "price_49_rub": settings.continuation_pack_price_rub == 49,
        "receipt_name_exact": settings.payment_receipt_item_name == "Пакет доступа Ravuna",
        "receipt_tax_none": settings.payment_receipt_tax == "none",
    }
    with database.read() as connection:
        pending_delivery = int(connection.execute(
            "SELECT COUNT(*) FROM payment_orders WHERE status='delivery_pending'"
        ).fetchone()[0])
        rejected = int(connection.execute(
            "SELECT COUNT(*) FROM payment_events WHERE status='rejected'"
        ).fetchone()[0])
    sandbox_ready = bool(
        checks["provider_selected"]
        and checks["sandbox_mode"]
        and checks["credentials_present"]
        and checks["result_url_https"]
        and checks["result_url_exact"]
        and checks["success_url_https"]
        and checks["fail_url_https"]
        and checks["success_url_exact"]
        and checks["fail_url_exact"]
        and checks["return_urls_without_query"]
        and checks["listener_loopback"]
        and checks["webhook_path_exact"]
        and checks["currency_rub"]
        and checks["price_49_rub"]
        and checks["receipt_name_exact"]
        and checks["receipt_tax_none"]
        and settings.payments_enabled
        and settings.payment_webhook_enabled
        and not settings.max_poll_observe_only
    )
    return {
        "mode": settings.robokassa_mode,
        "payments_enabled": settings.payments_enabled,
        "webhook_enabled": settings.payment_webhook_enabled,
        "refund_execution_enabled": settings.payment_refunds_enabled,
        "checks": checks,
        "sandbox_runtime_ready": sandbox_ready,
        "production_runtime_ready": bool(
            sandbox_ready
            and settings.robokassa_mode == "production"
            and settings.robokassa_production_approved
        ),
        "pending_legacy_original_deliveries": pending_delivery,
        "rejected_callback_events": rejected,
        "notes": [
            "This command does not create a payment or call the refund API.",
            "Cabinet moderation and a recorded sandbox E2E remain separate gates.",
        ],
    }


def _percentile(values: Iterable[int], percentile: float) -> int | None:
    ordered = sorted(int(value) for value in values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, int((len(ordered) * percentile + 0.999999) - 1)))
    return ordered[index]


def pilot_report(settings: Settings, database: Database) -> dict[str, Any]:
    invited_platform_ids = settings.max_pilot_user_ids[: settings.pilot_user_limit]
    user_ids: list[str] = []
    if invited_platform_ids:
        placeholders = ",".join("?" for _ in invited_platform_ids)
        with database.read() as connection:
            user_ids = [
                str(row[0])
                for row in connection.execute(
                    f"SELECT id FROM users WHERE platform='max' AND platform_user_id IN ({placeholders})",
                    invited_platform_ids,
                ).fetchall()
            ]
    event_counts: dict[str, int] = {}
    unique_event_users: dict[str, int] = {}
    attempts: list[Any] = []
    payment_rows: list[Any] = []
    error_rows: list[Any] = []
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        with database.read() as connection:
            event_rows = connection.execute(
                f"""SELECT pe.event_type,COUNT(*) AS count,
                            COUNT(DISTINCT COALESCE(ds.user_id,ga.user_id,gi.user_id)) AS users
                     FROM product_events pe
                     LEFT JOIN demo_sessions ds ON ds.id=pe.session_id
                     LEFT JOIN generation_attempts ga ON ga.id=pe.attempt_id
                     LEFT JOIN gallery_items gi ON gi.id=pe.gallery_item_id
                     WHERE COALESCE(ds.user_id,ga.user_id,gi.user_id) IN ({placeholders})
                     GROUP BY pe.event_type""",
                user_ids,
            ).fetchall()
            event_counts = {str(row["event_type"]): int(row["count"]) for row in event_rows}
            unique_event_users = {str(row["event_type"]): int(row["users"]) for row in event_rows}
            attempts = connection.execute(
                f"""SELECT status,duration_ms,estimated_cost,error_type,correction,source_version_id
                     FROM generation_attempts WHERE user_id IN ({placeholders})""",
                user_ids,
            ).fetchall()
            payment_rows = connection.execute(
                f"""SELECT status,amount_minor,refunded_amount_minor
                     FROM payment_orders WHERE user_id IN ({placeholders})""",
                user_ids,
            ).fetchall()
            error_rows = connection.execute(
                f"""SELECT COALESCE(error_type,'unknown') AS error_type,COUNT(*) AS count
                     FROM generation_attempts
                     WHERE user_id IN ({placeholders}) AND status NOT IN ('pending','processing','succeeded')
                     GROUP BY COALESCE(error_type,'unknown')""",
                user_ids,
            ).fetchall()
    succeeded = [row for row in attempts if row["status"] == "succeeded"]
    durations = [int(row["duration_ms"]) for row in succeeded if row["duration_ms"]]
    generation_cost = sum(float(row["estimated_cost"] or 0) for row in succeeded)
    initial_cost = sum(
        float(row["estimated_cost"] or 0)
        for row in succeeded if not row["correction"] and not row["source_version_id"]
    )
    correction_cost = sum(
        float(row["estimated_cost"] or 0) for row in succeeded if row["correction"]
    )
    repeat_cost = sum(
        float(row["estimated_cost"] or 0)
        for row in succeeded if not row["correction"] and row["source_version_id"]
    )
    recognized_states = {
        "paid", "delivery_pending", "delivered", "refund_pending",
        "partially_refunded", "refunded",
    }
    revenue_minor = sum(
        int(row["amount_minor"]) for row in payment_rows if row["status"] in recognized_states
    )
    refunded_minor = sum(int(row["refunded_amount_minor"] or 0) for row in payment_rows)
    net_revenue = (revenue_minor - refunded_minor) / 100
    commission = revenue_minor / 100 * settings.robokassa_commission_percent / 100
    tax_assumption_percent = 4.0
    tax_estimate = max(0.0, net_revenue * tax_assumption_percent / 100)
    photo = event_counts.get("photo_uploaded", 0)
    result = event_counts.get("result_delivered", 0)
    users_count = len(user_ids)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "configured_active_pilot_prefix_only",
        "privacy": "No platform IDs, prompts or file paths are included.",
        "stage": settings.pilot_user_limit,
        "handlers_enabled": not settings.max_poll_observe_only,
        "invited_users": len(invited_platform_ids),
        "activated_users": users_count,
        "funnel": {
            "start": event_counts.get("start", 0),
            "photo": photo,
            "prompt": event_counts.get("prompt_submitted", 0),
            "generation_started": event_counts.get("processing_started", 0),
            "result": result,
            "unlock": event_counts.get("unlock_clicked", 0),
            "payment_started": event_counts.get("payment_started", 0),
            "payment_success": event_counts.get("payment_confirmed", 0),
            "payment_failed": event_counts.get("payment_failed", 0),
            "original_delivered": event_counts.get("original_delivered", 0),
            "refund_requested": event_counts.get("refund_started", 0),
        },
        "first_result_completion_rate": round(result / photo, 4) if photo else None,
        "latency_ms": {
            "average": round(sum(durations) / len(durations)) if durations else None,
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
        },
        "usage": {
            "successful_generations": len(succeeded),
            "corrections": event_counts.get("correction_started", 0),
            "repeats": event_counts.get("repeat_started", 0),
            "corrections_per_activated_user": round(
                event_counts.get("correction_started", 0) / users_count, 2
            ) if users_count else None,
            "repeats_per_activated_user": round(
                event_counts.get("repeat_started", 0) / users_count, 2
            ) if users_count else None,
            "feedback_positive": event_counts.get("feedback_positive", 0),
            "feedback_negative": event_counts.get("feedback_negative", 0),
            "unique_result_users": unique_event_users.get("result_delivered", 0),
        },
        "errors": {
            "count": sum(int(row["count"]) for row in error_rows),
            "by_type": {str(row["error_type"]): int(row["count"]) for row in error_rows},
        },
        "economics": {
            "estimated_openai_cost_rub": round(generation_cost, 2),
            "initial_generation_cost_rub": round(initial_cost, 2),
            "correction_cost_rub": round(correction_cost, 2),
            "repeat_cost_rub": round(repeat_cost, 2),
            "gross_revenue_rub": round(revenue_minor / 100, 2),
            "refunded_rub": round(refunded_minor / 100, 2),
            "net_revenue_rub": round(net_revenue, 2),
            "robokassa_fee_percent_configured": settings.robokassa_commission_percent,
            "robokassa_fees_estimated_rub": round(commission, 2),
            "npd_tax_assumption_percent": tax_assumption_percent,
            "tax_estimate_rub": round(tax_estimate, 2),
            "gross_margin_estimate_rub": round(
                net_revenue - generation_cost - commission - tax_estimate, 2
            ),
            "warning": "Fee, tax, VPS, support and refund reserve require verified commercial inputs.",
        },
        "attribution_note": (
            "Start events are counted only when they carry a session/attempt/gallery relation; "
            "pre-upload starts are not reliably cohort-attributable in schema v8."
        ),
    }
