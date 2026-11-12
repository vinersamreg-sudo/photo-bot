import hashlib
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch

from app.backup import BackupError, BackupManager
from app.commerce import SMALL_PACKAGE, MEDIUM_PACKAGE, LARGE_PACKAGE
from app.database import Database
from app.max_transport import MaxTransportError, SingleInstanceLock
from app.payments import build_payment_service
from app.ru_recovery import READBACK, S3, _restore, _scheduled_day, export_migration, promote, watchdog
from app.ru_db_recovery import _prove, publish as publish_checkpoint, restore_latest


class MemoryStore:
    def __init__(self):
        self.catalog = {"points": [], "scheduled_day": None}
        self.slots = {}
        self.corrupt_readback = None
        self.fail_catalog = False
        self.etag = 0

    def get_catalog(self, _work):
        return self.catalog, self.etag

    def put_catalog(self, catalog, etag, _work):
        if self.fail_catalog or etag != self.etag:
            raise BackupError("catalog write failed")
        self.catalog = catalog
        self.etag += 1

    def put(self, slot, path):
        self.slots[slot] = path.read_bytes()

    def get(self, slot, path, expected_size):
        raw = self.slots[slot]
        if len(raw) != expected_size:
            raise BackupError("size mismatch")
        path.write_bytes(b"x" * len(raw) if slot == self.corrupt_readback else raw)

    def discard(self, slot):
        del self.slots[slot]


class RuRecoveryTests(TestCase):
    def test_offsite_verify_failures_and_manual_run_preserve_good_points(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = MemoryStore()
            scheduled = "2026-11-12"
            instant = datetime(2026, 11, 12, 3, tzinfo=timezone.utc)
            verify = []

            def publish(data, schedule=False):
                archive = root / READBACK
                archive.write_bytes(data)
                def verified(digest):
                    verify.append(digest)
                    return {"state_sha256": hashlib.sha256(data).hexdigest()}
                return promote(store, root, archive, verified, scheduled=schedule, now=lambda: instant)

            publish(b"first", scheduled)
            publish(b"second")
            before = store.catalog
            store.corrupt_readback = "c"
            with self.assertRaises(BackupError):
                publish(b"corrupt", "2026-11-13")
            self.assertEqual(store.catalog, before)
            store.corrupt_readback = None
            archive = root / READBACK
            archive.write_bytes(b"unrestorable")
            with self.assertRaises(BackupError):
                promote(store, root, archive, lambda _: self.fail_restore(), scheduled="2026-11-13")
            self.assertEqual(store.catalog, before)
            store.fail_catalog = True
            with self.assertRaises(BackupError):
                publish(b"not-published", "2026-11-13")
            self.assertEqual(store.catalog, before)
            self.assertEqual(len(store.slots), 3)
            store.fail_catalog = False
            with self.assertRaises(BackupError):
                publish(b"manual-rotation")
            publish(b"third", "2026-11-13")
            self.assertEqual(len(store.slots), 2)
            self.assertEqual(len(store.catalog["points"]), 2)
            self.assertEqual(set(store.catalog["scheduled_days"]), {scheduled, "2026-11-13"})
            def verify_independent(_):
                return {"state_sha256": hashlib.sha256(b"first").hexdigest()}
            watchdog(store, root, verify_independent, instant)
            with self.assertRaises(BackupError):
                watchdog(store, root, verify_independent, instant.replace(day=14))

    def test_daily_and_weekly_share_slots_without_eviction_on_duplicate(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = MemoryStore()
            initial = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
            archive = root / READBACK
            for i in range(20):
                instant = initial + timedelta(days=i)
                archive.write_bytes(f"ciphertext {i}".encode())
                state = f"{min(i,18) + 1:064x}"
                promote(store, root, archive, lambda _h: {"state_sha256": state},
                        scheduled=instant.date().isoformat(), now=lambda: instant)
                self.assertLessEqual(len(store.slots), 6)
            days = store.catalog["scheduled_days"]
            self.assertEqual(set(days), {"2026-11-08", "2026-11-15", "2026-11-17",
                                         "2026-11-18", "2026-11-19", "2026-11-20"})
            self.assertEqual(days["2026-11-19"], days["2026-11-20"])
            self.assertEqual(len(store.slots), 5)
            calls = []
            def prove(digest):
                calls.append(digest)
                return {"state_sha256": state}
            watchdog(store, root, prove, instant)
            self.assertEqual(len(calls), 1)
            with self.assertRaises(BackupError):
                watchdog(store, root, lambda _h: self.fail_restore(), instant)

    def test_legacy_catalog_retains_both_during_failed_transition(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = MemoryStore()
            start = datetime(2026, 11, 12, 3, tzinfo=timezone.utc)
            store.slots = {"a": b"first", "b": b"second"}
            store.catalog["points"] = [
                {"slot": slot, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                 "verified_at": start.isoformat()}
                for slot, data in store.slots.items()
            ]
            archive = root / READBACK
            archive.write_bytes(b"new")
            with self.assertRaises(BackupError):
                promote(store, root, archive, lambda _h: self.fail_restore(),
                        scheduled="2026-11-12", now=lambda: start)
            self.assertEqual(set(store.catalog), {"points", "scheduled_day"})
            self.assertEqual(store.slots["a"], b"first")
            self.assertEqual(store.slots["b"], b"second")

    def test_checkpoint_publishes_only_after_readback_and_proof(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "checkpoint.enc"
            store = MemoryStore()
            store.catalog = {"version": 1, "points": []}
            start = datetime(2026, 11, 12, 3, tzinfo=timezone.utc)
            for index in range(2):
                archive.write_bytes(f"checkpoint {index}".encode())
                publish_checkpoint(store, root, archive, start + timedelta(minutes=index * 15),
                                   lambda received: self.assertEqual(received.read_bytes(), archive.read_bytes()))
            previous = store.catalog
            store.corrupt_readback = "c"
            with self.assertRaises(BackupError):
                publish_checkpoint(store, root, archive, start + timedelta(minutes=30), lambda _p: None)
            self.assertEqual(store.catalog, previous)
            self.assertEqual(store.slots["a"], b"checkpoint 0")
            self.assertEqual(store.slots["b"], b"checkpoint 1")

    @staticmethod
    def fail_restore():
        raise BackupError("synthetic restore failure")

    def test_scheduler_evidence_is_not_inferred_from_time_alone(self):
        start = datetime(2026, 11, 11, 23, 0, 10, tzinfo=timezone.utc)
        trigger = int(start.timestamp() - 5) * 1_000_000
        with patch("app.ru_recovery.subprocess.run", return_value=SimpleNamespace(stdout="t 0")):
            self.assertIsNone(_scheduled_day(start))
        with patch("app.ru_recovery.subprocess.run", return_value=SimpleNamespace(stdout=f"t {trigger}")):
            self.assertEqual(_scheduled_day(start), "2026-11-12")

    def test_s3_requires_abort_rule_and_rejects_expiring_verified_slots(self):
        store = S3("https://storage.example.test", "synthetic-bucket", "test")
        abort_rule = {"Status": "Enabled", "Filter": {"Prefix": "ravuna/slots/"},
                      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}
        with patch.object(store, "_api", side_effect=[{}, {"Rules": [abort_rule]}]):
            store.ensure_bounded()
        with patch.object(store, "_api", side_effect=[{}, {"Rules": [
            abort_rule, {"Status": "Enabled", "Expiration": {"Days": 1}},
        ]}]), self.assertRaises(BackupError):
            store.ensure_bounded()

    @skipUnless(shutil.which("openssl"), "OpenSSL CLI required")
    def test_wal_media_and_late_payment_replay_on_restored_database(self):
        from tests.test_payments import PaymentTests

        fixture = PaymentTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        (fixture.base / ".deploy-sha").write_text("a" * 40, encoding="ascii")
        self.addCleanup(fixture.service.provider.close)
        wallet = fixture.demo.commerce
        source_balance = wallet.balance(fixture.user_id).available
        with TemporaryDirectory() as folder:
            scratch = Path(folder)
            original = fixture.settings.database_path
            with sqlite3.connect(original) as writer:
                self.addCleanup(writer.close)
                self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("BEGIN")
                writer.execute("SELECT count(*) FROM payment_orders").fetchone()
                orders = [fixture.service.create_order(
                    fixture.user_id, None, "migrate-" + package.code,
                    account_purchase=True, product_code=package.code,
                ) for package in (SMALL_PACKAGE, MEDIUM_PACKAGE, LARGE_PACKAGE)]
                self.assertTrue(Path(str(original) + "-wal").is_file())
                # Synthetic schema-16 archive: never infer new terms after restore.
                with fixture.database.transaction() as connection:
                    connection.execute("DROP TABLE gallery_retention_terms")
                    connection.execute("DROP TABLE payment_retention_terms")
                    connection.execute("DELETE FROM schema_migrations WHERE version=17")
                with SingleInstanceLock(fixture.base / "data/max-polling.lock"):
                    with self.assertRaises(MaxTransportError):
                        export_migration(fixture.base, scratch, "synthetic recovery phrase only")
                report = export_migration(fixture.base, scratch, "synthetic recovery phrase only")
                writer.rollback()
            self.assertEqual(report["counts"]["payment_orders"], 3)
            self.assertEqual(report["counts"]["gallery_versions"], 2)
            self.assertEqual(report["source_schema"], 16)
            manager = BackupManager(original, scratch, 14)
            restored = scratch / "restored"
            verified = _restore(manager, report["archive"], "synthetic recovery phrase only", restored, report["sha256"])
            self.assertEqual(verified, {k: v for k, v in report.items() if k != "archive"})
            self.assertTrue(any(path.is_file() for path in (restored / "private_storage/users").rglob("*")))
            restored_db = Database(restored / "database/photo_bot.sqlite3")
            settings = replace(fixture.settings, base_dir=restored)
            payments = build_payment_service(settings, restored_db, clock=fixture.clock)
            self.addCleanup(payments.provider.close)
            for order in orders:
                self.assertEqual(payments.payment_redirect_form(order.public_token).fields,
                                 fixture.service.payment_redirect_form(order.public_token).fields)
            fixture.clock.advance(minutes=31)
            for order in orders:
                for duplicate in (False, True):
                    result = payments.process_webhook(fixture.signed_callback(order), method="POST",
                                                      path="/payments/robokassa/result")
                    self.assertTrue(result.accepted)
                    self.assertEqual(result.duplicate, duplicate)
            with restored_db.read() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM gallery_retention_terms").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM payment_retention_terms").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT count(*) FROM continuation_pack_grants").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT count(*) FROM payment_receipts").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT available_generation_credits FROM user_credit_accounts WHERE user_id=?",
                                                    (fixture.user_id,)).fetchone()[0], source_balance + 124)
            self.assertEqual(wallet.balance(fixture.user_id).available, source_balance)

    @skipUnless(shutil.which("openssl"), "OpenSSL CLI required")
    def test_encrypted_sqlite_checkpoint_restores_wal_order_without_rewriting_source(self):
        from tests.test_payments import PaymentTests

        fixture = PaymentTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        phrase = "synthetic checkpoint phrase only"
        source = fixture.settings.database_path
        with TemporaryDirectory() as folder, closing(sqlite3.connect(source)) as reader:
            work = Path(folder)
            self.assertEqual(reader.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM payment_orders").fetchone()
            fixture.service.create_order(fixture.user_id, None, "checkpoint-order", account_purchase=True)
            manager = BackupManager(source, work, 14)
            metadata = manager.create(phrase)
            archive = work / metadata["backup_name"]
            store = MemoryStore()
            store.catalog = {"version": 1, "points": []}
            result = publish_checkpoint(store, work, archive, datetime.now(timezone.utc),
                                        lambda path: _prove(path, work / "checked.sqlite3", phrase))
            self.assertEqual(result["checkpoints"], 1)
            destination = work / "isolated.sqlite3"
            restore_latest(store, work, destination, phrase)
            with Database(destination).read() as restored:
                self.assertEqual(restored.execute("SELECT count(*) FROM payment_orders").fetchone()[0], 1)
            self.assertEqual(reader.execute("SELECT count(*) FROM payment_orders").fetchone()[0], 0)
            reader.rollback()

    @skipUnless(shutil.which("openssl"), "OpenSSL CLI required")
    def test_missing_required_gallery_media_blocks_full_restore(self):
        from tests.test_payments import PaymentTests

        fixture = PaymentTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        (fixture.base / ".deploy-sha").write_text("a" * 40, encoding="ascii")
        with fixture.database.read() as c:
            original = c.execute("SELECT original_path FROM gallery_versions WHERE original_path IS NOT NULL").fetchone()[0]
        Path(original).unlink()
        with TemporaryDirectory() as folder, self.assertRaises(BackupError):
            export_migration(fixture.base, Path(folder), "synthetic recovery phrase only")
