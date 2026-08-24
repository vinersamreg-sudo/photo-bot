from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from app.database import CURRENT_SCHEMA_VERSION, Database, schema_version


PAID_STATUSES = (
    "paid",
    "delivery_pending",
    "delivered",
    "refund_pending",
    "partially_refunded",
    "refunded",
)
SUPPORTED_SOURCE_VERSIONS = (CURRENT_SCHEMA_VERSION - 1, CURRENT_SCHEMA_VERSION)


class MigrationCopyError(RuntimeError):
    pass


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _safe_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"blob_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    return value


def _fingerprint_rows(rows: Iterable[sqlite3.Row]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        encoded = json.dumps(
            [_safe_value(value) for value in row],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def _commerce_snapshot(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    paid_placeholders = ",".join("?" for _ in PAID_STATUSES)
    queries: dict[str, tuple[str, tuple[object, ...]]] = {
        "users": ("SELECT * FROM users ORDER BY id", ()),
        "paid_orders": (
            f"SELECT * FROM payment_orders WHERE status IN ({paid_placeholders}) ORDER BY id",
            PAID_STATUSES,
        ),
        "grants": ("SELECT * FROM continuation_pack_grants ORDER BY id", ()),
        "credit_lots": ("SELECT * FROM generation_credit_lots ORDER BY id", ()),
        "credit_ledger": ("SELECT * FROM credit_ledger ORDER BY id", ()),
        "unlock_entitlements": ("SELECT * FROM unlock_entitlements ORDER BY id", ()),
        "small_49_grants": (
            """SELECT g.* FROM continuation_pack_grants AS g
               JOIN payment_orders AS o ON o.id=g.payment_order_id
               WHERE o.amount_minor=4900
               ORDER BY g.id""",
            (),
        ),
    }
    snapshot: dict[str, dict[str, object]] = {}
    for name, (query, params) in queries.items():
        count, fingerprint = _fingerprint_rows(connection.execute(query, params))
        snapshot[name] = {"count": count, "fingerprint": fingerprint}
    return snapshot


def _assert_commerce_invariants(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationCopyError("foreign key check failed on migrated copy")
    invalid_lots = int(
        connection.execute(
            """SELECT COUNT(*) FROM generation_credit_lots
               WHERE available_credits<0 OR reserved_credits<0 OR consumed_credits<0
                  OR refunded_credits<0
                  OR available_credits+reserved_credits+consumed_credits+refunded_credits
                     <> granted_credits"""
        ).fetchone()[0]
    )
    duplicate_grants = int(
        connection.execute(
            """SELECT COUNT(*) FROM (
                   SELECT payment_order_id FROM continuation_pack_grants
                   GROUP BY payment_order_id HAVING COUNT(*)>1
               )"""
        ).fetchone()[0]
    )
    invalid_small_grants = int(
        connection.execute(
            """SELECT COUNT(*) FROM continuation_pack_grants AS g
               JOIN payment_orders AS o ON o.id=g.payment_order_id
               WHERE o.amount_minor=4900
                 AND (g.generation_credit_quantity<>2 OR g.unlock_entitlement_quantity<>1)"""
        ).fetchone()[0]
    )
    if invalid_lots or duplicate_grants or invalid_small_grants:
        raise MigrationCopyError("commerce invariants failed on migrated copy")


def verify_migration_copy(source_path: Path, copy_path: Path) -> dict[str, object]:
    source_path = source_path.resolve()
    copy_path = copy_path.resolve()
    if not source_path.is_file():
        raise MigrationCopyError("source database is missing")
    if copy_path.exists():
        raise MigrationCopyError("migration copy path must not already exist")
    copy_path.parent.mkdir(parents=True, exist_ok=True)

    source = _read_only_connection(source_path)
    destination = sqlite3.connect(copy_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    before = _read_only_connection(copy_path)
    try:
        source_schema = schema_version(before)
        if source_schema not in SUPPORTED_SOURCE_VERSIONS:
            raise MigrationCopyError("source database schema is not supported for this deploy")
        before_snapshot = _commerce_snapshot(before)
    finally:
        before.close()

    Database(copy_path)

    after = _read_only_connection(copy_path)
    try:
        target_schema = schema_version(after)
        quick_check = str(after.execute("PRAGMA quick_check").fetchone()[0])
        after_snapshot = _commerce_snapshot(after)
        _assert_commerce_invariants(after)
    finally:
        after.close()

    if target_schema != CURRENT_SCHEMA_VERSION:
        raise MigrationCopyError("migration copy did not reach the required schema")
    if quick_check != "ok":
        raise MigrationCopyError("quick_check failed on migrated copy")
    if before_snapshot != after_snapshot:
        raise MigrationCopyError("critical commerce data changed during copy migration")

    return {
        "status": "PASS",
        "source_schema": source_schema,
        "target_schema": target_schema,
        "quick_check": quick_check,
        "counts": {
            name: int(values["count"]) for name, values in after_snapshot.items()
        },
        "commerce_rows_preserved": True,
        "small_49_grants_preserved": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a Ravuna schema migration on an isolated SQLite copy."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--copy", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_migration_copy(args.source, args.copy)
    except (MigrationCopyError, sqlite3.Error, OSError, RuntimeError):
        print(json.dumps({"status": "FAIL", "reason": "migration_copy_check_failed"}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
