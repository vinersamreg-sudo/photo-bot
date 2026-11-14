"""Offline recovery gate. Never serves, publishes, writes SQLite or calls a provider."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from app.backup import BackupError, BackupManager
from app.database import ReadOnlyDatabase
from app.payment_admin import ECONOMICALLY_PAID_STATES, payment_reconciliation_summary


MEDIA = "private_storage/users/"
REFERENCES = {
    "gallery_items": ("original_source_path", "secondary_source_path", "cover_preview_path",
                      "preview_small_path", "preview_large_path"),
    "gallery_versions": ("source_path", "secondary_source_path", "original_path", "preview_watermarked_path"),
    "generation_attempts": ("source_path", "secondary_source_path", "original_result_path", "demo_result_path"),
    "demo_sessions": ("source_file_path", "secondary_source_file_path"),
    "pending_edit_requests": ("primary_source_path", "secondary_source_path"),
}


def media_references(database, source_users):
    paths = {}
    source_users = Path(source_users)
    with ReadOnlyDatabase(database).read() as c:
        c.execute("BEGIN")
        for table, columns in REFERENCES.items():
            checks = ({"source_file_path": "source_sha256", "secondary_source_file_path": "secondary_source_sha256"}
                      if table == "demo_sessions" else
                      {"secondary_source_path": "secondary_source_sha256"} if table == "gallery_items" else {})
            select = ("id", *columns, *checks.values())
            for row in c.execute(f"SELECT {','.join(select)} FROM {table}"):
                for column in columns:
                    if not row[column]:
                        continue
                    try:
                        relative = Path(row[column]).relative_to(source_users)
                    except ValueError:
                        raise BackupError("Required media path is outside source storage") from None
                    if not relative.parts or ".." in relative.parts:
                        raise BackupError("Unsafe required media path")
                    digest = row[checks[column]] if column in checks else None
                    # Some historic records do not have a usable stored source
                    # digest; the verified archive manifest remains mandatory.
                    if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                        digest = None
                    paths[(table, row["id"], column)] = (MEDIA + relative.as_posix(),
                                                          digest.lower() if digest else None)
    return paths


def validate_media(database, source_users, hashes, *, previous_db=None):
    refs = media_references(database, source_users)
    old = media_references(previous_db, source_users) if previous_db else None
    if old is not None:
        old_paths, new_paths = {p for p, _ in old.values()}, {p for p, _ in refs.values()}
        if old_paths - new_paths:
            raise BackupError("Newer SQLite retires media; do not resurrect the older tree")
    for identity, (path, expected) in refs.items():
        actual = hashes.get(path)
        if not isinstance(actual, str) or not re.fullmatch(r"[a-f0-9]{64}", actual):
            raise BackupError("SQLite references media absent from the verified full archive")
        if expected and actual != expected:
            raise BackupError("SQLite source digest does not match the archive")
        if old is not None and old.get(identity) != (path, expected) and expected != actual:
            raise BackupError("Newer SQLite media reference is not proven by the older archive")
    return {"media_references": len(refs), "distinct_media": len({p for p, _ in refs.values()})}


def verify_materialized_media(root, hashes):
    for name, digest in hashes.items():
        if not name.startswith(MEDIA):
            continue
        relative = Path(name)
        path = root / relative
        if (relative.is_absolute() or ".." in relative.parts or not path.is_file()
                or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
                or BackupManager._sha256(path) != digest):
            raise BackupError("Materialized media failed digest verification")


def _stamp(raw):
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise BackupError("Recovery time must include an offset")
    return dt


def validate_finance(database):
    if payment_reconciliation_summary(ReadOnlyDatabase(database))["status"] == "CRITICAL":
        raise BackupError("Local payment ledger cannot be reconciled")
    with ReadOnlyDatabase(database).read() as c:
        if c.execute("""SELECT 1 FROM generation_credit_lots l
            WHERE l.available_credits + l.reserved_credits + l.consumed_credits + l.refunded_credits
                      <> l.granted_credits
            UNION ALL SELECT 1 FROM continuation_pack_grants g
            JOIN generation_credit_lots l ON l.id=g.credit_lot_id
            WHERE l.user_id<>g.user_id OR l.source_payment_order_id IS NOT g.payment_order_id
                  OR l.granted_credits<>g.generation_credit_quantity
            UNION ALL SELECT 1 FROM user_credit_accounts a
            WHERE a.available_generation_credits <> COALESCE(
                (SELECT SUM(l.available_credits) FROM generation_credit_lots l WHERE l.user_id=a.user_id),0)
               OR a.reserved_generation_credits <> COALESCE(
                (SELECT SUM(l.reserved_credits) FROM generation_credit_lots l WHERE l.user_id=a.user_id),0)
            UNION ALL SELECT 1 FROM payment_orders o WHERE o.refunded_amount_minor <> COALESCE(
                (SELECT SUM(r.amount_minor) FROM refund_intents r
                 WHERE r.order_id=o.id AND r.status='succeeded'),0)
               OR o.refunded_amount_minor <> COALESCE(
                (SELECT SUM(r.amount_minor) FROM payment_receipts r
                 WHERE r.order_id=o.id AND r.receipt_type='refund' AND r.status='refund_confirmed'),0)
            LIMIT 1""").fetchone():
            raise BackupError("Recovered credits, package or refund ledger does not reconcile")


def reconcile_provider(database, evidence, *, from_time, through_time):
    """Verify a complete, operator-authenticated Robokassa export; no auto-grants.

    An export's completeness/merchant provenance is a separate operator gate.
    Externally verified amounts are in minor units. No identifiers are returned.
    """
    if (set(evidence) != {"coverage_start", "coverage_end", "operations"}
            or _stamp(evidence["coverage_start"]) > from_time
            or _stamp(evidence["coverage_end"]) < through_time):
        raise BackupError("Provider evidence does not cover the recovery window")
    validate_finance(database)
    remote = {}
    for row in evidence["operations"]:
        if set(row) != {"invoice", "amount_minor", "refunded_minor", "currency"}:
            raise BackupError("Invalid provider operation")
        key, amount, refund = row["invoice"], row["amount_minor"], row["refunded_minor"]
        if (type(key) is not int or key <= 0 or key in remote or type(amount) is not int or amount <= 0
                or type(refund) is not int or not 0 <= refund <= amount or row["currency"] != "RUB"):
            raise BackupError("Invalid or duplicate provider operation")
        remote[key] = (amount, refund, row["currency"])
    matched = set()
    with ReadOnlyDatabase(database).read() as c:
        c.execute("BEGIN")
        orders = c.execute("""SELECT o.*, u.id AS account FROM payment_orders o
            LEFT JOIN users u ON u.id=o.user_id WHERE o.provider='robokassa'""")
        for order in orders:
            invoice = order["provider_invoice_id"]
            paid_at = _stamp(order["paid_at"]) if order["paid_at"] else None
            if invoice in remote:
                if (not order["account"] or not paid_at or order["status"] not in ECONOMICALLY_PAID_STATES
                        or remote[invoice] != (order["amount_minor"], order["refunded_amount_minor"] or 0,
                                               order["currency"])):
                    raise BackupError("Provider confirmation or refund disagrees with recovered order")
                matched.add(invoice)
            elif paid_at is not None and from_time <= paid_at <= through_time:
                raise BackupError("Recovered payment is missing from provider evidence")
        if matched != remote.keys():
            raise BackupError("Confirmed provider payment has no recovered order/account; keep sales closed")
    return {"matched_provider_operations": len(matched), "finance": "PASS"}
