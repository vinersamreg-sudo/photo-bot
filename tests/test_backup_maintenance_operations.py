import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from app.backup import BackupError, BackupManager
from app.config import Settings, load_settings
from app.database import Database
from app.image_service import build_demo_service
from app.maintenance import run_maintenance
from app.operations import collect_launch_status


class BackupMaintenanceOperationsTests(TestCase):
    def base(self, directory: str) -> Path:
        base = Path(directory)
        for name in ("data", "logs", "temp"):
            (base / name).mkdir()
        return base

    @staticmethod
    def fake_openssl(*args: str, passphrase: str) -> None:
        del passphrase
        source = Path(args[args.index("-in") + 1])
        target = Path(args[args.index("-out") + 1])
        shutil.copyfile(source, target)

    def test_encrypted_backup_is_created_and_restore_tested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = self.base(directory)
            database = Database(base / "data" / "photo_bot.sqlite3")
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO product_events(event_type,created_at) VALUES('start',?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
            manager = BackupManager(database.path, base / "data" / "backups", 14)
            with patch.object(BackupManager, "_openssl", side_effect=self.fake_openssl):
                created = manager.create("correct-horse-battery-staple")
                restored = manager.restore_test(
                    Path(created["backup_name"]), "correct-horse-battery-staple"
                )
            self.assertTrue(created["encrypted"])
            self.assertTrue(restored["restore_ok"])
            self.assertEqual(restored["quick_check"], "ok")
            self.assertGreaterEqual(restored["migration"], 6)
            self.assertNotIn("passphrase", json.dumps(created))
            with self.assertRaisesRegex(BackupError, "file name"):
                manager.restore_test(
                    base / "outside.sqlite3.enc", "correct-horse-battery-staple"
                )

    def test_synthetic_recovery_bundle_restores_database_and_private_storage(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            application = root / "application"
            application.mkdir()
            base = self.base(str(application))
            database = Database(base / "data" / "photo_bot.sqlite3")
            private_file = base / "data" / "users" / "synthetic" / "preview.jpg"
            private_file.parent.mkdir(parents=True)
            private_file.write_bytes(b"synthetic-private-storage")
            revision = "a" * 40
            (base / ".deploy-sha").write_text(revision, encoding="utf-8")
            (base / ".env").write_text("SECRET=must-not-be-backed-up", encoding="utf-8")
            manager = BackupManager(database.path, base / "data" / "backups", 14)
            restore_root = root / "isolated-restore"
            with patch.object(BackupManager, "_openssl", side_effect=self.fake_openssl):
                created = manager.create_recovery_bundle(
                    "correct-horse-battery-staple"
                )
                restored = manager.restore_recovery_bundle(
                    Path(created["recovery_bundle_name"]),
                    "correct-horse-battery-staple",
                    restore_root=restore_root,
                )
            self.assertEqual(restored["overall"], "PASS")
            self.assertEqual(restored["sqlite_status"], "PASS")
            self.assertEqual(restored["missing_components"], [])
            self.assertEqual(restored["source_revision"], revision)
            manifest = json.loads(
                (restore_root / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["bundle_version"], 1)
            self.assertTrue(manifest["encrypted"])
            self.assertEqual(manifest["source_revision"], revision)
            self.assertEqual(
                set(manifest["included_components"]),
                {"database/photo_bot.sqlite3", "private_storage/users"},
            )
            self.assertEqual(
                (restore_root / "private_storage" / "users" / "synthetic" / "preview.jpg").read_bytes(),
                b"synthetic-private-storage",
            )
            self.assertFalse((restore_root / ".env").exists())
            self.assertFalse(created["secrets_included"])
            with self.assertRaisesRegex(BackupError, "separate"):
                manager.restore_recovery_bundle(
                    Path(created["recovery_bundle_name"]),
                    "correct-horse-battery-staple",
                    restore_root=base / "unsafe",
                )
            (manager.backup_dir / created["recovery_bundle_name"]).write_bytes(
                b"corrupted"
            )
            with patch.object(BackupManager, "_openssl", side_effect=self.fake_openssl):
                failed = manager.restore_recovery_bundle(
                    Path(created["recovery_bundle_name"]),
                    "correct-horse-battery-staple",
                )
            self.assertEqual(failed["overall"], "FAIL")
            self.assertEqual(failed["artifact_status"], "FAIL")
            self.assertEqual(failed["restore_status"], "FAIL")

    def test_real_openssl_synthetic_recovery_drill(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("OpenSSL is unavailable")
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            base = root / "application"
            base.mkdir()
            self.base(str(base))
            database = Database(base / "data" / "photo_bot.sqlite3")
            (base / ".deploy-sha").write_text("b" * 40, encoding="utf-8")
            source = base / "data" / "users" / "synthetic" / "source.bin"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"synthetic-recovery-proof")
            manager = BackupManager(database.path, base / "data" / "backups", 14)
            created = manager.create_recovery_bundle(
                "correct-horse-battery-staple"
            )
            report = manager.restore_recovery_bundle(
                Path(created["recovery_bundle_name"]),
                "correct-horse-battery-staple",
                restore_root=root / "restored",
            )
            self.assertEqual(report["overall"], "PASS")
            self.assertEqual(report["artifact_status"], "PASS")
            self.assertEqual(report["sqlite_status"], "PASS")

    def test_recovery_bundle_requires_a_source_revision(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            base = Path(parent) / "application"
            base.mkdir()
            self.base(str(base))
            database = Database(base / "data" / "photo_bot.sqlite3")
            manager = BackupManager(database.path, base / "data" / "backups", 14)
            with self.assertRaisesRegex(BackupError, "source revision"):
                manager.create_recovery_bundle("correct-horse-battery-staple")

    def test_recovery_manifest_rejects_missing_components(self) -> None:
        with self.assertRaisesRegex(BackupError, "components"):
            BackupManager._validate_recovery_manifest({
                "bundle_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_revision": "c" * 40,
                "included_components": ["database/photo_bot.sqlite3"],
                "encrypted": True,
            })

    def test_cleanup_has_dry_run_and_preserves_referenced_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = self.base(directory)
            settings = Settings(
                "", "fake", "test", base,
                cleanup_temp_retention_hours=1,
                cleanup_orphan_grace_hours=1,
                demo_min_request_interval_seconds=1,
            )
            source = base / "input.png"
            Image.new("RGB", (40, 40), "white").save(source)
            service = build_demo_service(settings, provider_name="fake")
            session = service.start_session("test", "pilot", source)
            old = datetime.now(timezone.utc).timestamp() - 7200
            os.utime(session.source_path, (old, old))
            metadata = session.source_path.parent.parent / "metadata.json"
            os.utime(metadata, (old, old))
            orphan = settings.users_dir / "orphan.bin"
            orphan.write_bytes(b"orphan")
            os.utime(orphan, (old, old))
            temporary = settings.temp_dir / "stale.tmp"
            temporary.write_bytes(b"temporary")
            os.utime(temporary, (old, old))

            dry = run_maintenance(settings, execute=False)
            self.assertEqual(dry["orphan_candidate_count"], 1)
            self.assertEqual(dry["temp_candidate_count"], 1)
            self.assertTrue(orphan.exists())
            self.assertTrue(temporary.exists())

            executed = run_maintenance(settings, execute=True)
            self.assertEqual(executed["removed_file_count"], 2)
            self.assertFalse(orphan.exists())
            self.assertFalse(temporary.exists())
            self.assertTrue(session.source_path.exists())
            self.assertTrue(metadata.exists())

    def test_launch_status_is_privacy_safe_and_covers_readiness_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = self.base(directory)
            settings = Settings(
                "openai-secret", "gpt-image-2", "test", base,
                image_provider="openai",
                max_bot_token="max-secret",
                max_transport_mode="polling",
                max_owner_user_ids=("owner-private",),
                disk_min_free_mb=1,
            )
            database = Database(settings.database_path)
            now = datetime.now(timezone.utc).isoformat()
            with database.transaction() as connection:
                connection.execute(
                    """INSERT INTO max_transport_state(name,value,updated_at)
                       VALUES('poll_last_success','ok',?)""",
                    (now,),
                )
            settings.backup_dir_path.mkdir(parents=True)
            name = "pixora-test.sqlite3.enc"
            for filename, value in (
                ("latest.json", {"backup_name": name, "created_at": now}),
                ("restore_status.json", {"backup_name": name, "restore_ok": True}),
                ("offsite_status.json", {"backup_name": name, "copied_at": now}),
            ):
                (settings.backup_dir_path / filename).write_text(
                    json.dumps(value), encoding="utf-8"
                )
            (settings.data_dir / "maintenance_last.json").write_text(
                json.dumps({"completed_at": now, "remaining_orphan_count": 0}),
                encoding="utf-8",
            )
            report = collect_launch_status(settings, online=False)
            self.assertTrue(report["readiness"]["operational_ready"])
            self.assertFalse(report["readiness"]["site_moderation_ready"])
            self.assertFalse(report["readiness"]["robokassa_sandbox_ready"])
            self.assertFalse(report["readiness"]["robokassa_production_ready"])
            self.assertFalse(report["readiness"]["owner_e2e_ready"])
            self.assertFalse(report["readiness"]["pilot_5_ready"])
            self.assertFalse(report["readiness"]["public_launch_ready"])
            public_report = collect_launch_status(
                replace(
                    settings,
                    max_public_access_enabled=True,
                    max_poll_observe_only=False,
                ),
                online=False,
            )
            self.assertTrue(public_report["runtime"]["public_access_enabled"])
            self.assertTrue(public_report["readiness"]["public_launch_ready"])
            self.assertEqual(public_report["readiness"]["mode"], "public")
            serialized = json.dumps(report)
            for secret in ("openai-secret", "max-secret", "owner-private"):
                self.assertNotIn(secret, serialized)
            for section in (
                "runtime", "max", "provider", "database", "storage", "backup",
                "cleanup", "processing", "monitoring", "today", "site",
                "owner_e2e", "readiness",
            ):
                self.assertIn(section, report)
            self.assertFalse(report["provider"]["live_balance_claimed"])
            self.assertIsNone(report["provider"]["openai_balance_usd"])
            self.assertTrue(report["provider"]["confirmation_stale"])
            self.assertGreaterEqual(report["monitoring"]["p1_count"], 1)

    def test_launch_status_never_mutates_a_live_processing_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = self.base(directory)
            settings = Settings("", "fake", "test", base)
            source = base / "live.png"
            Image.new("RGB", (40, 40), "white").save(source)
            service = build_demo_service(settings, provider_name="fake")
            session = service.start_session("test", "pilot", source)
            now = datetime.now(timezone.utc).isoformat()
            with service.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO generation_attempts(
                           id,idempotency_key,session_id,user_id,prompt,status,started_at,
                           provider,model,source_path,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "live-attempt", "live-event", session.session_id,
                        session.user_id, "test", "processing", now,
                        "fake", "fake", str(session.source_path), now,
                    ),
                )
            before_sha = hashlib.sha256(settings.database_path.read_bytes()).hexdigest()
            collect_launch_status(settings, online=False)
            after_sha = hashlib.sha256(settings.database_path.read_bytes()).hexdigest()
            with service.database.read() as connection:
                status = connection.execute(
                    "SELECT status FROM generation_attempts WHERE id='live-attempt'"
                ).fetchone()[0]
            self.assertEqual(status, "processing")
            self.assertEqual(after_sha, before_sha)

    def test_pilot_allowlist_enables_only_requested_prefix(self) -> None:
        settings = load_settings(
            environ={
                "APP_ENV": "test",
                "MAX_OWNER_USER_IDS": "owner",
                "MAX_PILOT_USER_IDS": "p1,p2,p3,p4,p5,p6",
                "PILOT_USER_LIMIT": "5",
            }
        )
        self.assertEqual(settings.max_allowed_user_ids, ("owner", "p1", "p2", "p3", "p4", "p5"))

    def test_pilot_allowlist_fails_closed_when_short_or_contains_owner(self) -> None:
        with self.assertRaisesRegex(ValueError, "shorter"):
            load_settings(
                environ={
                    "APP_ENV": "test",
                    "MAX_OWNER_USER_IDS": "owner",
                    "MAX_PILOT_USER_IDS": "p1,p2",
                    "PILOT_USER_LIMIT": "5",
                }
            )
        with self.assertRaisesRegex(ValueError, "must not contain"):
            load_settings(
                environ={
                    "APP_ENV": "test",
                    "MAX_OWNER_USER_IDS": "owner",
                    "MAX_PILOT_USER_IDS": "owner,p1,p2,p3,p4",
                    "PILOT_USER_LIMIT": "5",
                }
            )

    def test_backup_workflow_only_verifies_encrypted_artifacts_off_host(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "backup.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("backup-create --passphrase-stdin", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("backup-mark-offsite", workflow)
        self.assertIn("sha256sum -c -", workflow)
        self.assertNotIn("backup-restore-test", workflow)
        self.assertNotIn("recovery-restore-test", workflow)
        self.assertNotIn("maintenance-cleanup --execute", workflow)
        self.assertNotIn('--passphrase "$PASSPHRASE"', workflow)
        self.assertIn("recovery-create --passphrase-stdin", workflow)
        self.assertIn("recovery-mark-offsite", workflow)
        self.assertIn("artifact/*.enc", workflow)

        uploaded = workflow.index("actions/upload-artifact@v4")
        confirmed = workflow.index("backup-mark-offsite")
        self.assertLess(uploaded, confirmed)
