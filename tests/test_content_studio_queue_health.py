from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.content_studio.cli import main
from app.content_studio.config import ContentStudioSettings
from app.content_studio.growth import FullAutoGrowthEngine
from app.content_studio.publisher import PlatformPublisher
from app.content_studio.service import ContentStudioService


ROOT = Path(__file__).resolve().parents[1]


class ContentStudioQueueHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = ContentStudioSettings(
            base_dir=root,
            database_path=root / "content.sqlite3",
            storage_dir=root / "storage",
            approved_assets_dir=ROOT / "marketing/assets/approved",
            content_library_path=ROOT / "marketing/content/library.json",
            production_database_path=root / "missing.sqlite3",
            full_auto_enabled=True,
            publishing_enabled=True,
            max_publishing_enabled=True,
        )
        self.service = ContentStudioService(settings, publishers={
            "max": PlatformPublisher(platform="max", publishing_enabled=True),
            "vk": PlatformPublisher(platform="vk", publishing_enabled=False),
        })
        self.engine = FullAutoGrowthEngine(self.service)
        self.now = datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def test_fully_used_library_is_degraded_not_pass(self):
        used = {item.sha256 for item in self.engine.library}
        with patch.object(self.service.repository, "reserved_asset_checksums", return_value=used):
            result = self.engine.run(now=self.now, apply=True)
        self.assertEqual(result["queue"]["created_posts"], [])
        self.assertEqual(result["queue_health"]["status"], "DEGRADED")
        self.assertEqual(result["queue_health"]["reason"], "candidate_pool_exhausted")
        self.assertEqual(result["queue_health"]["days_queued"], 0)
        self.assertEqual(len(result["queue_health"]["missing_slots"]), 7)
        self.assertEqual(result["publication"]["published"], [])
        self.assertEqual(self.service.repository.status()["posts"], 0)
        self.assertEqual(self.service.repository.status()["publication_attempts"], 0)

    def test_dry_run_predicts_seven_days_without_persisting_posts(self):
        before = self.service.repository.status()
        result = self.engine.maintain_queue(now=self.now, apply=False)
        self.assertEqual(result["health"]["status"], "HEALTHY")
        self.assertEqual(result["health"]["days_queued"], 7)
        self.assertEqual(result["health"]["basis"], "preview")
        self.assertEqual(self.service.repository.status(), before)

    def test_persisted_queue_covers_horizon_and_repeat_is_idempotent(self):
        first = self.engine.maintain_queue(now=self.now, apply=True)
        second = self.engine.maintain_queue(now=self.now, apply=True)
        self.assertEqual(first["health"]["status"], "HEALTHY")
        self.assertEqual(first["health"]["days_queued"], 7)
        self.assertEqual(first["health"]["basis"], "persisted")
        posts = self.service.repository.queue(limit=100)
        self.assertTrue(all(datetime.fromisoformat(p["scheduled_time"]) > self.now for p in posts))
        self.assertEqual(min(p["scheduled_time"] for p in posts), "2026-10-08T15:00:00+00:00")
        self.assertEqual(second["created_posts"], [])
        self.assertEqual(second["health"]["status"], "HEALTHY")

    def test_far_future_slot_does_not_hide_gap_or_disabled_vk(self):
        def existing(platform, slot):
            return {"id": "synthetic"} if platform == "vk" or slot.startswith("2026-10-14") else None
        with patch.object(self.service.repository, "scheduled_slot_is_owned", side_effect=existing):
            health = self.engine.queue_health(now=self.now, skipped=[])
        self.assertEqual(health["status"], "DEGRADED")
        self.assertEqual(health["platform_days"], {"max": 0})
        self.assertEqual(len(health["missing_slots"]), 6)

    def test_unowned_scheduled_rows_cannot_make_health_green(self):
        self.engine.maintain_queue(now=self.now, apply=True)
        conn = self.service.repository.connect()
        try:
            conn.execute("DELETE FROM content_publication_slots")
        finally:
            conn.close()
        health = self.engine.queue_health(now=self.now, skipped=[])
        self.assertEqual(health["status"], "DEGRADED")
        self.assertEqual(health["days_queued"], 0)

    def test_next_slot_before_and_at_1900_samara(self):
        for moment, first_day in ((datetime(2026, 10, 7, 14, 59, tzinfo=timezone.utc), "2026-10-07"),
                                  (datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc), "2026-10-08")):
            with self.subTest(moment=moment):
                health = self.engine.queue_health(now=moment, skipped=[])
                self.assertTrue(health["missing_slots"][0]["scheduled_time"].startswith(first_day))

    def test_applied_run_rechecks_queue_after_publication(self):
        self.engine.maintain_queue(now=self.now, apply=True)
        def remove_next_slot(**_):
            conn = self.service.repository.connect()
            try:
                conn.execute("UPDATE demo_posts SET publish_status='archived' WHERE scheduled_time=?",
                             ((self.now + timedelta(days=1)).replace(hour=15).isoformat(),))
            finally:
                conn.close()
            return {"due": [], "published": [], "failed": []}
        with patch.object(self.engine, "publish_due", side_effect=remove_next_slot):
            result = self.engine.run(now=self.now, apply=True)
        self.assertEqual(result["queue"]["health"]["status"], "HEALTHY")
        self.assertEqual(result["queue_health"]["status"], "DEGRADED")

    def test_cli_applied_degraded_is_nonzero_but_preview_is_inspectable(self):
        for apply, state, expected in ((True, "DEGRADED", 1), (False, "DEGRADED", 0),
                                       (True, "HEALTHY", 0)):
            with self.subTest(apply=apply, state=state), contextlib.redirect_stdout(io.StringIO()):
                with patch("app.content_studio.cli.ContentStudioService") as service_class:
                    service_class.return_value.full_auto_run.return_value = {
                        "queue_health": {"status": state}, "publication": {"failed": []},
                    }
                    with patch("app.content_studio.cli.load_dotenv"):
                        self.assertEqual(main(["content", "auto-run"] + (["--apply"] if apply else [])), expected)

    def test_cli_publication_failure_still_returns_nonzero(self):
        with contextlib.redirect_stdout(io.StringIO()), patch("app.content_studio.cli.ContentStudioService") as service_class:
            service_class.return_value.full_auto_run.return_value = {
                "queue_health": {"status": "HEALTHY"}, "publication": {"failed": [{"error": "TimeoutError"}]},
            }
            with patch("app.content_studio.cli.load_dotenv"):
                self.assertEqual(main(["content", "auto-run", "--apply"]), 1)


if __name__ == "__main__":
    unittest.main()
