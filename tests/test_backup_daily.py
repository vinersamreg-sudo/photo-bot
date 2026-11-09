import errno
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.backup import BackupError, BackupManager
from app.backup_daily import DailyRecovery, LEASE_SECONDS, SAMARA
from tests.test_backup_streaming import SECRET, plain_stream


class DailyRecoveryTests(TestCase):
    def setUp(self):
        work = self.enterContext(TemporaryDirectory())
        root = Path(work)
        db = root / "data/photo_bot.sqlite3"
        db.parent.mkdir()
        (root / ".deploy-sha").write_text("a" * 40)
        self.manager = BackupManager(db, db.parent / "backups", 14)
        files = [self.manager.users_dir / filename for filename in ("original.jpg", "preview.jpg")]
        self.manager.users_dir.mkdir()
        for file in files:
            file.write_bytes(file.name.encode() * 100)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE schema_migrations(version INTEGER);
                INSERT INTO schema_migrations VALUES (16);
                CREATE TABLE payment_orders(id INTEGER PRIMARY KEY);
                INSERT INTO payment_orders VALUES (1);
                CREATE TABLE gallery_items(id INTEGER,purged_at TEXT);
                INSERT INTO gallery_items VALUES(1,NULL);
                CREATE TABLE gallery_versions(gallery_item_id INTEGER,original_path TEXT,preview_watermarked_path TEXT);
            """)
            conn.execute("INSERT INTO gallery_versions VALUES(1,?,?)", tuple(map(str, files)))
        self.enterContext(patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream))

        def fake_openssl(*args, passphrase):
            shutil.copyfile(args[args.index("-in") + 1], args[args.index("-out") + 1])

        self.enterContext(patch.object(BackupManager, "_openssl", side_effect=fake_openssl))
        self.enterContext(patch("app.backup.shutil.disk_usage", return_value=SimpleNamespace(free=30 * 1024**3)))
        self.when = datetime(2026, 11, 9, 23, tzinfo=timezone.utc)
        self.enterContext(patch("app.backup._now", side_effect=self.tick))
        self.enterContext(patch("app.backup_daily._samara_today",
                                side_effect=lambda: self.when.astimezone(SAMARA).date()))

    def tick(self):
        self.when += timedelta(microseconds=1)
        return self.when

    def points(self):
        return sorted(self.manager.backup_dir.glob("ravuna-recovery-*.tar.gz.enc"))

    def accept(self, cycle, ready):
        return cycle.accept("github-actions:1:1", ready["backup_sha"], ready["recovery_sha"])

    def day(self, n):
        self.when += timedelta(days=1)
        cycle = DailyRecovery(self.manager, f"{n}-1")
        ready = cycle.create(SECRET)
        self.assertEqual(cycle.create(SECRET), ready)
        self.assertEqual(ready["restore_method"], "bounded_file_restore")
        self.assertFalse(list(cycle.stage.rglob(".media-restore")))
        self.accept(cycle, ready)
        self.assertFalse(cycle.stage.exists())
        for _ in range(2):
            self.manager.prune_recovery_by_count(
                2, confirmed_backup=ready["recovery_name"], dry_run=False,
            )
        return ready

    def test_ten_days_and_repeated_prune_keep_latest_two(self):
        history = []
        for n in range(10):
            history.append(self.day(n)["recovery_name"])
            self.assertEqual([p.name for p in self.points()], history[-2:])
        with self.assertRaisesRegex(BackupError, "already exists"):
            DailyRecovery(self.manager, "999-1").create(SECRET)

    def test_ten_failed_upload_or_readback_runs_preserve_two(self):
        self.day(1)
        self.day(2)
        before = self.points()
        for n in range(3, 13):
            self.when += timedelta(days=1)
            cycle = DailyRecovery(self.manager, f"{n}-1")
            ready = cycle.create(SECRET)
            with self.assertRaisesRegex(BackupError, "readback"):
                cycle.accept("github-actions:1:1", ready["backup_sha"], "0" * 64)
            self.assertTrue(cycle.cleanup()["cleaned"])
            self.assertFalse(cycle.cleanup()["cleaned"])
            self.assertFalse(cycle.stage.exists())
            self.assertEqual(self.points(), before)

    def test_parallel_run_and_abandoned_partial_stage(self):
        old = DailyRecovery(self.manager, "1-1")
        new = DailyRecovery(self.manager, "2-1")
        old.create(SECRET)
        (old.stage / "interrupted-partial").write_bytes(b"synthetic")
        with self.assertRaisesRegex(BackupError, "staging lease"):
            new.create(SECRET)
        self.assertFalse(new.cleanup()["cleaned"])
        with patch("app.backup_daily.time.time", return_value=time.time() + LEASE_SECONDS + 1):
            new.create(SECRET)
        self.assertFalse((new.stage / "interrupted-partial").exists())
        self.assertFalse(old.cleanup()["cleaned"])
        new.cleanup()

    def test_failed_restore_and_budget_leave_no_pending_files(self):
        cycle = DailyRecovery(self.manager, "1-1")
        with patch("app.backup.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(BackupError, "no_space_left"):
                cycle.create(SECRET)
        with patch("app.backup.DAILY_BACKUP_BUDGET", 1):
            with self.assertRaisesRegex(BackupError, "backup_budget"):
                cycle.create(SECRET)
        with patch.object(BackupManager, "_restore_one_at_a_time",
                          side_effect=OSError(errno.ENOSPC, "synthetic")):
            with self.assertRaises(OSError):
                cycle.create(SECRET)
        self.assertFalse(cycle.stage.exists())
        self.assertEqual(self.points(), [])

    def test_corrupted_candidate_and_interrupted_publication(self):
        cycle = DailyRecovery(self.manager, "1-1")
        ready = cycle.create(SECRET)
        cipher = cycle.stage / ready["recovery_name"]
        original = cipher.read_bytes()
        cipher.write_bytes(original + b"damaged")
        with self.assertRaisesRegex(BackupError, "changed"):
            self.accept(cycle, ready)
        cipher.write_bytes(original)
        with patch("app.backup_daily._sync_directory", side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):
                self.accept(cycle, ready)
        self.assertEqual(self.points(), [])
        self.accept(cycle, ready)
        self.assertEqual(len(self.points()), 1)
