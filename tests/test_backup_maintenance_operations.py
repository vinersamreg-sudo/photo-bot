import json
import os
import shutil
import tempfile
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

    def test_launch_status_is_privacy_safe_and_covers_readiness_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = self.base(directory)
            settings = Settings(
                "openai-secret", "gpt-image-2", "test", base,
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
            self.assertFalse(report["readiness"]["public_launch_ready"])
            serialized = json.dumps(report)
            for secret in ("openai-secret", "max-secret", "owner-private"):
                self.assertNotIn(secret, serialized)
            for section in (
                "runtime", "max", "provider", "database", "storage", "backup",
                "cleanup", "processing", "today", "readiness",
            ):
                self.assertIn(section, report)

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
            collect_launch_status(settings, online=False)
            with service.database.read() as connection:
                status = connection.execute(
                    "SELECT status FROM generation_attempts WHERE id='live-attempt'"
                ).fetchone()[0]
            self.assertEqual(status, "processing")

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

    def test_backup_workflow_restores_before_offsite_confirmation_and_cleanup(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "backup.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("backup-create --passphrase-stdin", workflow)
        self.assertGreaterEqual(workflow.count("backup-restore-test"), 2)
        self.assertIn("Independently restore-test copied artifact", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("backup-mark-offsite", workflow)
        self.assertIn("maintenance-cleanup --execute", workflow)
        self.assertIn("launch-status --strict", workflow)
        self.assertNotIn('--passphrase "$PASSPHRASE"', workflow)

        uploaded = workflow.index("actions/upload-artifact@v4")
        confirmed = workflow.index("backup-mark-offsite")
        cleaned = workflow.index("maintenance-cleanup --execute")
        self.assertLess(uploaded, confirmed)
        self.assertLess(confirmed, cleaned)
