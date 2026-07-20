import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.config import Settings
from app.database import SCHEMA, Database
from app.demo_service import DemoService
from app.gallery import GalleryService
from app.image_provider import FakeImageProvider
from app.storage import PrivateStorage
from app.watermark import WatermarkService


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class GalleryTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        for name in ("data", "logs", "temp"):
            (self.base / name).mkdir()
        self.settings = Settings(
            "", "fake-image-edit-v1", "test", self.base,
            demo_min_request_interval_seconds=1,
            trash_retention_days=1,
        )
        self.clock = Clock()
        self.database = Database(self.settings.database_path)
        self.storage = PrivateStorage(self.settings.users_dir, 15 * 1024 * 1024)
        self.source = self.base / "source.png"
        Image.new("RGB", (400, 300), "#e5edf5").save(self.source)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,?,?,?)",
                ("gallery-user", "max", "gallery-platform-user", self.clock().isoformat()),
            )
        self.gallery = GalleryService(
            self.database, self.storage, self.settings, self.clock
        )

    def test_gallery_collections_tags_search_preferences_and_continue(self) -> None:
        collection = self.gallery.create_collection("gallery-user", "Работа")
        item = self.gallery.create_item(
            "gallery-user", "Резюме для BMW", self.source, "resume", collection
        )
        self.gallery.rename_item("gallery-user", item.id, "Резюме BMW")
        self.gallery.set_tags("gallery-user", item.id, ["BMW", "Резюме", "Студия"])
        self.gallery.set_item_favorite("gallery-user", item.id, True)
        found = self.gallery.search(
            "gallery-user", query="BMW", scenario_id="resume",
            folder_id=collection, favorite=True,
            created_from="2026-07-15T00:00:00+00:00",
            created_to="2026-07-16T00:00:00+00:00",
        )
        self.assertEqual([row.id for row in found], [item.id])
        self.assertEqual(self.gallery.list_collections("gallery-user")[0]["item_count"], 1)
        self.gallery.update_preferences(
            "gallery-user",
            favorite_style="деловой",
            favorite_background="светлый",
            favorite_scenarios=["resume"],
            recent_scenarios=["resume", "enhance"],
        )
        preferences = self.gallery.get_preferences("gallery-user")
        self.assertEqual(preferences["favorite_style"], "деловой")
        self.assertEqual(preferences["recent_scenarios"], ["resume", "enhance"])
        continued = self.gallery.continue_work("gallery-user")
        self.assertEqual(continued[0].id, item.id)
        self.assertIsNone(continued[1])

    def test_demo_attempts_become_versions_and_correction_uses_parent_original(self) -> None:
        service = DemoService(
            self.settings,
            self.database,
            self.storage,
            WatermarkService("ОБРАЗЕЦ", 1024, "JPEG", 82),
            FakeImageProvider(),
            clock=self.clock,
        )
        session = service.start_session("max", "version-user", self.source)
        first_result = service.generate(session.session_id, "Светлый фон", "version-1")
        with self.database.read() as connection:
            item_id = connection.execute(
                "SELECT gallery_item_id FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
        versions = service.gallery.list_versions(session.user_id, item_id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].version_number, 1)
        self.assertIsNone(versions[0].original_path)
        self.clock.advance(2)
        service.generate(
            session.session_id,
            "Сделай лицо естественнее",
            "version-2",
            correction=True,
            parent_version_id=versions[0].id,
        )
        versions = service.gallery.list_versions(session.user_id, item_id)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[1].parent_version_id, versions[0].id)
        with self.database.read() as connection:
            first_original = Path(connection.execute(
                "SELECT original_path FROM gallery_versions WHERE id=?", (versions[0].id,)
            ).fetchone()[0])
        self.assertEqual(versions[1].source_path, first_original)
        self.assertEqual(versions[1].source_version_id, versions[0].id)
        self.assertIn("Сделай лицо естественнее", versions[1].effective_prompt)
        self.assertEqual(versions[1].edit_plan.mode, "correction")
        self.clock.advance(2)
        with self.database.transaction() as connection:
            service.commerce.adjust_generation_credits(
                connection,
                user_id=session.user_id,
                delta=1,
                reason="third gallery lineage version",
                idempotency_key="gallery-third-version-credit",
            )
        service.generate(
            session.session_id,
            "Новый случайный вариант",
            "version-3",
            repeat=True,
            parent_version_id=versions[1].id,
        )
        versions = service.gallery.list_versions(session.user_id, item_id)
        self.assertEqual(len(versions), 3)
        self.assertEqual(versions[2].parent_version_id, versions[1].id)
        self.assertEqual(versions[2].edit_plan.mode, "repeat")
        self.assertEqual(
            versions[2].edit_plan.requested_changes,
            versions[1].edit_plan.requested_changes,
        )
        self.assertEqual(versions[2].source_path, versions[1].source_path)
        service.gallery.rate_version(session.user_id, versions[1].id, 5)
        service.gallery.set_version_favorite(session.user_id, versions[0].id, True)
        service.gallery.set_current_best(session.user_id, item_id, versions[0].id)
        opened, best = service.gallery.open_item(session.user_id, item_id)
        self.assertEqual(best.id, versions[0].id)
        self.assertTrue(service.gallery.search(session.user_id, favorite=True))
        self.assertFalse(hasattr(first_result, "original_path"))

    def test_soft_delete_then_cleanup_removes_files_and_history(self) -> None:
        service = DemoService(
            self.settings, self.database, self.storage,
            WatermarkService("ОБРАЗЕЦ", 1024, "JPEG", 82),
            FakeImageProvider(), clock=self.clock,
        )
        session = service.start_session("max", "delete-user", self.source)
        service.generate(session.session_id, "test", "delete-version")
        with self.database.read() as connection:
            item_id = connection.execute(
                "SELECT gallery_item_id FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
            root = Path(connection.execute(
                "SELECT storage_root_path FROM gallery_items WHERE id=?", (item_id,)
            ).fetchone()[0])
        service.delete_session(session.session_id)
        self.assertTrue(root.exists())
        self.assertEqual(service.gallery.search(session.user_id), [])
        self.assertEqual(len(service.gallery.search(session.user_id, deleted=True)), 1)
        service.gallery.restore(session.user_id, item_id)
        self.assertEqual(len(service.gallery.search(session.user_id)), 1)
        service.gallery.soft_delete(session.user_id, item_id)
        self.clock.advance(86401)
        self.assertEqual(service.gallery.purge_due(execute=False), [item_id])
        self.assertEqual(service.gallery.purge_due(execute=True), [item_id])
        self.assertFalse(root.exists())
        with self.database.read() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM gallery_items WHERE id=?", (item_id,)
            ).fetchone())
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM gallery_versions WHERE gallery_item_id=?", (item_id,)
            ).fetchone()[0], 0)

    def test_legacy_demo_rows_are_backfilled_by_migration_two(self) -> None:
        legacy_path = self.base / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(SCHEMA)
        timestamp = self.clock().isoformat()
        legacy_root = self.base / "legacy-session"
        (legacy_root / "source").mkdir(parents=True)
        source = legacy_root / "source" / "source.png"
        Image.new("RGB", (64, 64), "white").save(source)
        original = legacy_root / "original.png"
        preview = legacy_root / "preview.jpg"
        Image.new("RGB", (64, 64), "blue").save(original)
        Image.new("RGB", (64, 64), "gray").save(preview)
        connection.execute(
            "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,?,?,?)",
            ("legacy-user", "max", "legacy-platform", timestamp),
        )
        connection.execute(
            """INSERT INTO demo_sessions(
                   id,user_id,source_file_path,source_sha256,status,started_at,expires_at,
                   successful_generations,max_generations,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("legacy-session", "legacy-user", str(source), "hash", "active", timestamp,
             (self.clock()+timedelta(hours=1)).isoformat(), 1, 5, timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO generation_attempts(
                   id,idempotency_key,session_id,user_id,prompt,status,started_at,completed_at,
                   provider,model,source_path,original_result_path,demo_result_path,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("legacy-attempt", "legacy-event", "legacy-session", "legacy-user", "test",
             "succeeded", timestamp, timestamp, "fake", "fake-model", str(source),
             str(original), str(preview), timestamp),
        )
        connection.commit()
        connection.close()
        migrated = Database(legacy_path)
        with migrated.read() as check:
            self.assertIsNotNone(check.execute(
                "SELECT 1 FROM schema_migrations WHERE version=2"
            ).fetchone())
            item = check.execute("SELECT * FROM gallery_items").fetchone()
            version = check.execute("SELECT * FROM gallery_versions").fetchone()
            gallery = check.execute("SELECT * FROM galleries").fetchone()
            brain_migration = check.execute(
                "SELECT 1 FROM schema_migrations WHERE version=4"
            ).fetchone()
        self.assertEqual(item["generation_count"], 1)
        self.assertEqual(item["gallery_id"], gallery["id"])
        self.assertEqual(version["attempt_id"], "legacy-attempt")
        self.assertEqual(item["current_best_version_id"], version["id"])
        self.assertIsNotNone(brain_migration)
        self.assertTrue(version["edit_plan_json"])
