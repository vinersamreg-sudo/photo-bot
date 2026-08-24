from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase

from app.commerce import CommerceService, SMALL_PACKAGE
from app.database import (
    CURRENT_SCHEMA_VERSION,
    Database,
    UnsupportedSchemaVersionError,
    schema_version,
)
from scripts.check_migration_copy import MigrationCopyError, verify_migration_copy


class MigrationSafetyTests(TestCase):
    @staticmethod
    def _downgrade_commerce_schema_to_v13(path: Path) -> None:
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys=OFF;
                BEGIN IMMEDIATE;
                ALTER TABLE continuation_pack_grants RENAME TO continuation_pack_grants_new;
                ALTER TABLE unlock_entitlements RENAME TO unlock_entitlements_new;
                DROP INDEX IF EXISTS idx_pack_grants_user_time;
                DROP INDEX IF EXISTS idx_unlock_entitlements_user_status;
                DROP INDEX IF EXISTS idx_unlock_entitlements_payment_order;
                CREATE TABLE unlock_entitlements (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    source_payment_intent_id TEXT,
                    source_payment_order_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('available','reserved','consumed','cancelled','refunded')),
                    gallery_version_id TEXT REFERENCES gallery_versions(id) ON DELETE SET NULL,
                    reserved_at TEXT,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO unlock_entitlements SELECT * FROM unlock_entitlements_new;
                CREATE INDEX idx_unlock_entitlements_user_status
                ON unlock_entitlements(user_id,status,created_at);
                CREATE TABLE continuation_pack_grants (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    payment_order_id TEXT NOT NULL UNIQUE,
                    payment_intent_id TEXT,
                    credit_lot_id TEXT NOT NULL UNIQUE REFERENCES generation_credit_lots(id),
                    entitlement_id TEXT NOT NULL UNIQUE REFERENCES unlock_entitlements(id),
                    generation_credit_quantity INTEGER NOT NULL CHECK(generation_credit_quantity=2),
                    unlock_entitlement_quantity INTEGER NOT NULL CHECK(unlock_entitlement_quantity=1),
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','refunded')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO continuation_pack_grants SELECT * FROM continuation_pack_grants_new;
                CREATE INDEX idx_pack_grants_user_time
                ON continuation_pack_grants(user_id,created_at DESC);
                DROP TABLE continuation_pack_grants_new;
                DROP TABLE unlock_entitlements_new;
                DELETE FROM schema_migrations WHERE version=14;
                COMMIT;
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _seed_paid_small_package(database: Database) -> None:
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)
        now_text = now.isoformat()
        user_id = "synthetic-user"
        order_id = "synthetic-small-order"
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO users(id,platform,platform_user_id,created_at)
                   VALUES(?,?,?,?)""",
                (user_id, "test", "synthetic-platform-user", now_text),
            )
            connection.execute(
                """INSERT INTO payment_orders(
                       id,public_token,attempt_id,version_id,user_id,provider,merchant_hash,
                       provider_invoice_id,amount_minor,currency,status,description,created_at,
                       updated_at,expires_at,paid_at,delivered_at,payment_purpose,product_code,
                       generation_credit_quantity,unlock_entitlement_quantity
                   ) VALUES(?,?,?,?,?,?,?,?,4900,'RUB','delivered',?,?,?,?,?,?,'processing_request',?,?,?)""",
                (
                    order_id,
                    "a" * 32,
                    "synthetic-attempt",
                    "synthetic-version",
                    user_id,
                    "robokassa",
                    "synthetic-merchant-hash",
                    1,
                    "Ravuna synthetic package",
                    now_text,
                    now_text,
                    now_text,
                    now_text,
                    now_text,
                    SMALL_PACKAGE.code,
                    SMALL_PACKAGE.generation_credits,
                    SMALL_PACKAGE.unlock_entitlements,
                ),
            )
            CommerceService(database, lambda: now).grant_continuation_pack(
                connection,
                user_id=user_id,
                payment_order_id=order_id,
                payment_intent_id=None,
                generation_credit_quantity=SMALL_PACKAGE.generation_credits,
                unlock_entitlement_quantity=SMALL_PACKAGE.unlock_entitlements,
            )

    def test_v13_to_v14_copy_migration_preserves_paid_small_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "production-copy-source.sqlite3"
            migrated = root / "production-copy-migrated.sqlite3"
            database = Database(source)
            self._seed_paid_small_package(database)
            self._downgrade_commerce_schema_to_v13(source)
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()

            report = verify_migration_copy(source, migrated)

            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["source_schema"], 13)
            self.assertEqual(report["target_schema"], CURRENT_SCHEMA_VERSION)
            self.assertEqual(report["quick_check"], "ok")
            self.assertEqual(report["counts"]["paid_orders"], 1)
            self.assertEqual(report["counts"]["small_49_grants"], 1)
            self.assertTrue(report["small_49_grants_preserved"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_sha)
            with closing(sqlite3.connect(source)) as connection:
                self.assertEqual(schema_version(connection), 13)
            with closing(sqlite3.connect(migrated)) as connection:
                self.assertEqual(schema_version(connection), CURRENT_SCHEMA_VERSION)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM continuation_pack_grants"
                    ).fetchone()[0],
                    1,
                )

    def test_unsupported_newer_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "unsupported.sqlite3"
            database = Database(source)
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                    (CURRENT_SCHEMA_VERSION + 1, "future", datetime.now(timezone.utc).isoformat()),
                )
            with self.assertRaises(UnsupportedSchemaVersionError):
                Database(source)
            with self.assertRaises(MigrationCopyError):
                verify_migration_copy(source, root / "copy.sqlite3")
