"""Immutable retention terms for new works; absence of evidence is a hold."""

from datetime import datetime, timedelta, timezone


POLICY_VERSION = 2
FREE_DAYS = 14
PAID_DAYS = 60
NOTICE = (
    "Для новых работ: бесплатно — 14 дней, оплачено — 60 дней. "
    "Для прежних работ и покупок сохраняются их условия."
)


def instant(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def active(settings, now):
    effective = instant(settings.result_retention_policy_effective_at)
    return effective if effective is not None and now >= effective else None


def pin(connection, item_id, settings, now):
    effective = active(settings, now)
    if effective is not None:
        connection.execute("INSERT INTO gallery_retention_terms VALUES(?,?,?,?,?)",
                           (item_id, POLICY_VERSION, effective.isoformat(), FREE_DAYS, PAID_DAYS))


def pin_order(connection, order_id, settings, now):
    effective = active(settings, now)
    if effective is not None:
        connection.execute("INSERT INTO payment_retention_terms VALUES(?,?,?,?)",
                           (order_id, POLICY_VERSION, effective.isoformat(), PAID_DAYS))


def applies(connection, settings, now):
    # Schema capability, not an activation switch. An empty or restored table
    # must not re-enable a legacy purge.
    from app.database import schema_version
    return schema_version(connection) >= 17


def require_execution(connection, settings, now):
    if not (applies(connection, settings, now) and settings.result_retention_enabled
            and active(settings, now)):
        raise RuntimeError("Versioned cleanup is not enabled")


def deadline(connection, result):
    """Return an expiry only for proven work and payment terms.

    Use the reservation actually consumed, not the user's latest purchase.
    Unlocks from an old, unknown or non-attributable purchase remain protected.
    """
    terms = connection.execute(
        "SELECT policy_version,effective_at,free_days,paid_days FROM gallery_retention_terms WHERE gallery_item_id=?",
        (result["gallery_item_id"],),
    ).fetchone()
    if terms is None:
        return None
    effective = instant(terms["effective_at"])
    created = instant(result["item_created_at"])
    completed = instant(result["completed_at"])
    if not effective or not created or not completed or not effective <= created <= completed:
        return None
    credit = connection.execute("""SELECT l.id,l.source_type,l.source_payment_order_id
        FROM generation_credit_reservations r JOIN generation_credit_lots l ON l.id=r.lot_id
        WHERE r.attempt_id=? AND r.user_id=? AND l.user_id=r.user_id AND r.status='consumed'""",
        (result["attempt_id"], result["user_id"]),
    ).fetchone()
    if not credit:
        return None
    if credit["source_type"] not in ("initial_free", "continuation_pack"):
        referral = connection.execute("""SELECT created_at FROM referral_bonus_grants
            WHERE credit_lot_id=? AND user_id=? AND bonus_type='edits'""",
            (credit["id"], result["user_id"])).fetchone()
        granted = instant(referral["created_at"]) if referral else None
        if not granted or granted < effective:
            return None
    paid_anchors = []
    paid_days = [terms["paid_days"]]
    free_anchors = [completed]

    def confirmed_order(order_id):
        row = connection.execute("""SELECT o.created_at,o.paid_at,o.delivered_at,o.status,t.policy_version,t.effective_at,t.paid_days
            FROM payment_orders o JOIN payment_retention_terms t ON t.payment_order_id=o.id
            WHERE o.id=? AND o.user_id=?""", (order_id, result["user_id"])).fetchone()
        if not row or row["policy_version"] != terms["policy_version"] or row["status"] not in (
            "paid", "delivered", "partially_refunded", "refunded"
        ):
            return False
        ordered, paid = instant(row["created_at"]), instant(row["paid_at"])
        order_terms = instant(row["effective_at"])
        # A package may predate this work or a later activation setting, but
        # its own stamped conditions and payment chronology must be proven.
        if not order_terms or not ordered or not paid or not order_terms <= ordered <= paid:
            return False
        paid_anchors.append(paid)
        paid_days.append(row["paid_days"])
        if row["delivered_at"] or row["status"] == "delivered":
            delivered = instant(row["delivered_at"])
            if not delivered or delivered < paid:
                return False
            paid_anchors.append(delivered)
        return True

    if credit["source_type"] == "continuation_pack" and not confirmed_order(credit["source_payment_order_id"]):
        return None
    if result["unlock_status"] != "demo":
        if result["unlock_status"] != "unlocked" or not result["delivery_count"]:
            return None
        unlocked, delivered = instant(result["unlocked_at"]), instant(result["last_delivered_at"])
        entitlement = connection.execute("""SELECT source_payment_order_id,consumed_at FROM unlock_entitlements
            WHERE id=? AND user_id=? AND gallery_version_id=? AND status='consumed'""",
            (result["unlock_entitlement_id"], result["user_id"], result["id"]),
        ).fetchone()
        if not unlocked or not delivered or not completed <= unlocked <= delivered:
            return None
        if not entitlement or instant(entitlement["consumed_at"]) != unlocked:
            return None
        if confirmed_order(entitlement["source_payment_order_id"]):
            # Consumption is the first confirmed delivery in the canonical MAX
            # flow; a later re-download only increments delivery_count.
            paid_anchors.append(unlocked)
        else:
            referral = connection.execute("""SELECT g.created_at FROM referral_bonus_grants g
                JOIN referral_bonus_entitlements e ON e.grant_id=g.id
                WHERE e.entitlement_id=? AND g.user_id=?""",
                (result["unlock_entitlement_id"], result["user_id"])).fetchone()
            granted = instant(referral["created_at"]) if referral else None
            if not granted or granted < effective:
                return None
            free_anchors.append(unlocked)
    # A confirmed original/target payment without matched delivery cannot be
    # treated as a free result. Pack checkout may bind an optional original.
    for row in connection.execute("""SELECT DISTINCT o.id,o.status,o.payment_purpose
        FROM payment_orders o LEFT JOIN payment_intents p ON p.id=o.intent_id
        WHERE o.user_id=? AND (o.version_id=? OR p.version_id=?)""",
        (result["user_id"], result["id"], result["id"]),
    ):
        if row["payment_purpose"] == "original_download" and result["unlock_status"] != "unlocked":
            return None
        if not confirmed_order(row["id"]):
            return None
    # Later proved obligations can extend this floor, never shorten it.
    return max([*free_anchors, *paid_anchors]) + timedelta(
        days=max(paid_days) if paid_anchors else terms["free_days"])
