import shutil
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
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
                return promote(store, root, archive, verify.append, scheduled=schedule, now=lambda: instant)

            publish(b"first", scheduled)
            publish(b"second")
            before = store.catalog
            store.corrupt_readback = "c"
            with self.assertRaises(BackupError):
                publish(b"corrupt")
            self.assertEqual(store.catalog, before)
            store.corrupt_readback = None
            archive = root / READBACK
            archive.write_bytes(b"unrestorable")
            with self.assertRaises(BackupError):
                promote(store, root, archive, lambda _: self.fail_restore())
            self.assertEqual(store.catalog, before)
            store.fail_catalog = True
            with self.assertRaises(BackupError):
                publish(b"not-published")
            self.assertEqual(store.catalog, before)
            self.assertEqual(len(store.slots), 3)
            store.fail_catalog = False
            publish(b"third")
            self.assertEqual(len(store.slots), 2)
            self.assertEqual(len(store.catalog["points"]), 2)
            self.assertEqual(store.catalog["scheduled_day"], scheduled)
            watchdog(store, root, instant)
            with self.assertRaises(BackupError):
                watchdog(store, root, instant.replace(day=13))

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
                with SingleInstanceLock(fixture.base / "data/max-polling.lock"):
                    with self.assertRaises(MaxTransportError):
                        export_migration(fixture.base, scratch, "synthetic recovery phrase only")
                report = export_migration(fixture.base, scratch, "synthetic recovery phrase only")
                writer.rollback()
            self.assertEqual(report["counts"]["payment_orders"], 3)
            self.assertEqual(report["counts"]["gallery_versions"], 2)
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
                self.assertEqual(connection.execute("SELECT count(*) FROM continuation_pack_grants").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT count(*) FROM payment_receipts").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT available_generation_credits FROM user_credit_accounts WHERE user_id=?",
                                                    (fixture.user_id,)).fetchone()[0], source_balance + 124)
            self.assertEqual(wallet.balance(fixture.user_id).available, source_balance)
