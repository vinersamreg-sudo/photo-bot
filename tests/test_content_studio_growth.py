from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from app.attribution import parse_start_payload
from app.content_studio.config import ContentStudioSettings
from app.content_studio.content_generator import ContentGenerator
from app.content_studio.models import TransformationType
from app.content_studio.storage import ContentStorage
from app.content_studio.growth import FullAutoGrowthEngine, _publish_timezone
from app.content_studio.service import ContentStudioService
from app.content_studio.video import (
    VerticalPairVideoSpec,
    VerticalVideoGenerator,
    VerticalVideoSpec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ContentStudioGrowthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.approved = self.root / "marketing" / "assets" / "approved"
        self.approved.mkdir(parents=True)
        self.runtime = self.root / "data" / "content_studio" / "storage"
        self.before = self.approved / "before.png"
        self.after = self.approved / "after.png"
        Image.new("RGB", (800, 1200), "#446688").save(self.before)
        Image.new("RGB", (800, 1200), "#886644").save(self.after)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_approved_asset_boundary_rejects_customer_or_random_paths(self) -> None:
        storage = ContentStorage(self.runtime, self.approved)
        outside = self.root / "data" / "users" / "customer.png"
        outside.parent.mkdir(parents=True)
        Image.new("RGB", (800, 1200), "red").save(outside)
        with self.assertRaisesRegex(ValueError, "marketing/assets/approved"):
            storage.import_image(outside, namespace="assets", item_id="one", stem="source")
        stored = storage.import_image(
            self.before, namespace="assets", item_id="two", stem="source"
        )
        self.assertTrue(storage.resolve(stored.relative_path).is_file())

    def test_settings_default_to_isolated_approved_root_and_daily_schedule(self) -> None:
        settings = ContentStudioSettings.from_environment(self.root, {})
        self.assertEqual(
            settings.approved_assets_dir,
            (self.root / "marketing/assets/approved").resolve(),
        )
        self.assertEqual(settings.daily_publish_time, "19:00")
        self.assertEqual(settings.daily_publish_timezone, "Europe/Samara")
        with self.assertRaisesRegex(ValueError, "outside customer"):
            ContentStudioSettings.from_environment(
                self.root,
                {"CONTENT_STUDIO_APPROVED_ASSETS_DIR": "data/users"},
            )

    def test_samara_publish_timezone_is_available_without_system_tzdata(self) -> None:
        zone = _publish_timezone("Europe/Samara")
        self.assertEqual(zone.utcoffset(datetime(2026, 8, 20)).total_seconds(), 4 * 60 * 60)

    def test_max_deep_link_uses_supported_start_payload_and_existing_parser(self) -> None:
        copy = ContentGenerator("https://max.ru/se13572368_bot").generate(
            TransformationType.REPLACE_BACKGROUND,
            post_id="growth-unit-01",
            platform="max",
        )
        query = parse_qs(urlsplit(copy.utm_url).query)
        self.assertEqual(query["start"], [copy.source_code])
        self.assertEqual(parse_start_payload(copy.source_code).campaign, copy.source_code[4:])
        self.assertLessEqual(len(copy.source_code), 128)

    def test_first_pack_has_ten_draft_units_and_no_customer_assets(self) -> None:
        value = json.loads((PROJECT_ROOT / "marketing/content/first-10.json").read_text("utf-8"))
        units = value["units"]
        self.assertEqual(len(units), 10)
        self.assertFalse(value["publication_enabled"])
        self.assertTrue(value["approval_required"])
        self.assertEqual(sum(unit["topic"] == "before_after" for unit in units), 3)
        self.assertEqual(sum(unit["topic"] == "replace_background" for unit in units), 2)
        self.assertTrue(all(unit["vk_status"] == unit["max_status"] == "draft" for unit in units))
        self.assertTrue(all("data/users" not in json.dumps(unit) for unit in units))

    def test_thirty_day_plan_covers_all_required_rubrics(self) -> None:
        value = json.loads((PROJECT_ROOT / "marketing/content/plan-30-days.json").read_text("utf-8"))
        self.assertEqual(len(value["daily_units"]), 30)
        rubrics = {unit["rubric"] for unit in value["daily_units"]}
        self.assertEqual(
            rubrics,
            {
                "before_after",
                "one_prompt_result",
                "replace_background",
                "replace_clothes",
                "remove_objects",
                "old_photo_improvement",
                "restoration",
                "quality_improvement",
                "useful_prompts",
                "common_user_mistakes",
            },
        )

    def test_vertical_video_command_is_clean_1080x1920_h264_and_bounded(self) -> None:
        generator = VerticalVideoGenerator(self.approved, self.runtime)
        output = self.runtime / "videos" / "growth-01.mp4"
        command = generator.command(
            VerticalVideoSpec(
                before=self.before,
                after=self.after,
                output=output,
                hook="Один запрос — результат",
            )
        )
        joined = " ".join(command)
        self.assertIn("1080x1920", joined)
        self.assertIn("xfade", joined)
        self.assertIn("libx264", command)
        self.assertIn("veryfast", command)
        self.assertIn("yuv420p", command)
        self.assertEqual(command[command.index("-t", command.index("-map")) + 1], "13")

    def test_vertical_video_rejects_unapproved_inputs(self) -> None:
        generator = VerticalVideoGenerator(self.approved, self.runtime)
        outside = self.root / "customer.png"
        Image.new("RGB", (800, 1200), "black").save(outside)
        with self.assertRaisesRegex(ValueError, "approved marketing assets"):
            generator.command(
                VerticalVideoSpec(
                    before=outside,
                    after=self.after,
                    output=self.runtime / "video.mp4",
                    hook="Hook",
                )
            )

    def test_pair_video_uses_low_memory_single_stream_rendering(self) -> None:
        generator = VerticalVideoGenerator(self.approved, self.runtime)
        command = generator.pair_command(
            VerticalPairVideoSpec(
                pair_card=self.before,
                output=self.runtime / "pair.mp4",
                hook="До и после",
                prompt_text="Запрос: улучшить фото",
            )
        )
        joined = " ".join(command)
        self.assertNotIn("xfade", joined)
        self.assertNotIn("zoompan", joined)
        self.assertIn("if(lt(t,4),0,iw/2)", joined)
        self.assertEqual(command[command.index("-threads") + 1], "1")

    def test_full_auto_apply_builds_an_idempotent_eight_day_queue(self) -> None:
        settings = ContentStudioSettings(
            base_dir=self.root / "content",
            database_path=self.root / "content" / "content.sqlite3",
            storage_dir=self.root / "content" / "storage",
            approved_assets_dir=PROJECT_ROOT / "marketing/assets/approved",
            publishing_enabled=True,
            full_auto_enabled=True,
            content_library_path=PROJECT_ROOT / "marketing/content/library.json",
            production_database_path=self.root / "missing-production.sqlite3",
        )
        service = ContentStudioService(settings)
        engine = FullAutoGrowthEngine(service)

        class FakeVideo:
            @staticmethod
            def render_pair(spec):
                spec.output.parent.mkdir(parents=True, exist_ok=True)
                spec.output.write_bytes(b"synthetic-video")
                return spec.output

        engine.video = FakeVideo()
        engine.quality.assess_video = lambda *args, **kwargs: {
            "decoded": True,
            "codec": "h264",
            "width": 1080,
            "height": 1920,
            "duration_seconds": 13.0,
        }
        now = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
        first = engine.maintain_queue(now=now, apply=True)
        second = engine.maintain_queue(now=now, apply=True)
        self.assertEqual(first["ideas_generated"], 18)
        self.assertEqual(
            first["idea_types"], ["before_after", "practical_tip", "prompt_example"]
        )
        self.assertEqual(len(first["created_posts"]), 19)
        self.assertEqual(first["days_queued"], 7)
        self.assertEqual(second["created_posts"], [])
        self.assertEqual(service.status()["posts"], 19)
        self.assertEqual(service.status()["assets"], 7)
        posts = service.repository.queue(limit=100)
        self.assertTrue(all(post["publish_status"] == "scheduled" for post in posts))
        self.assertEqual(sum(post["platform"] == "max" for post in posts), 8)
        self.assertEqual(sum(post["id"].startswith("vk-clip-") for post in posts), 8)
        self.assertEqual(sum(post["id"].startswith("vk-post-") for post in posts), 3)
        self.assertTrue(all(str(post["source_code"]).startswith("src_") for post in posts))

    def test_growth_funnel_reads_only_aggregate_attribution_without_db_mutation(self) -> None:
        production_db = self.root / "production.sqlite3"
        connection = sqlite3.connect(production_db)
        try:
            connection.execute(
                "CREATE TABLE attribution_events(campaign TEXT,event_type TEXT)"
            )
            connection.executemany(
                "INSERT INTO attribution_events(campaign,event_type) VALUES(?,?)",
                [
                    ("max-unit", "bot_started"),
                    ("max-unit", "photo_uploaded"),
                    ("max-unit", "payment_success"),
                    ("other", "bot_started"),
                ],
            )
            connection.commit()
        finally:
            connection.close()
        before = hashlib.sha256(production_db.read_bytes()).hexdigest()
        settings = ContentStudioSettings(
            base_dir=self.root / "content-ro",
            database_path=self.root / "content-ro" / "content.sqlite3",
            storage_dir=self.root / "content-ro" / "storage",
            approved_assets_dir=PROJECT_ROOT / "marketing/assets/approved",
            content_library_path=PROJECT_ROOT / "marketing/content/library.json",
            production_database_path=production_db,
        )
        engine = FullAutoGrowthEngine(ContentStudioService(settings))
        self.assertEqual(
            engine._funnel_by_campaign(["max-unit"]),
            {
                "max-unit": {
                    "bot_started": 1,
                    "photo_uploaded": 1,
                    "payment_success": 1,
                }
            },
        )
        self.assertEqual(hashlib.sha256(production_db.read_bytes()).hexdigest(), before)

    def test_full_auto_rebuilds_a_corrupt_partial_video_once(self) -> None:
        settings = ContentStudioSettings(
            base_dir=self.root / "content-repair",
            database_path=self.root / "content-repair" / "content.sqlite3",
            storage_dir=self.root / "content-repair" / "storage",
            approved_assets_dir=PROJECT_ROOT / "marketing/assets/approved",
            publishing_enabled=True,
            full_auto_enabled=True,
            content_library_path=PROJECT_ROOT / "marketing/content/library.json",
            production_database_path=self.root / "missing-production.sqlite3",
        )
        engine = FullAutoGrowthEngine(ContentStudioService(settings))
        now = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
        ideas = engine._idea_pool(now.date(), engine.optimization())
        asset = engine._select_assets(
            now.date(), 8, engine.optimization(), ideas=ideas
        )[0]
        post_id = f"vk-clip-{now.date().isoformat()}-{asset.id}"
        _, output = engine.service.storage.output_path(
            "videos", post_id, "vertical.mp4"
        )
        output.write_bytes(b"corrupt-partial")
        renders = []

        class FakeVideo:
            @staticmethod
            def render_pair(spec):
                renders.append(spec.output)
                spec.output.write_bytes(b"valid-video")
                return spec.output

        def assess_video(path, **_kwargs):
            if path.read_bytes() != b"valid-video":
                raise ValueError("invalid test video")
            return {"decoded": True, "codec": "h264", "width": 1080, "height": 1920}

        engine.video = FakeVideo()
        engine.quality.assess_video = assess_video
        result = engine.maintain_queue(now=now, apply=True)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(renders.count(output), 1)
        self.assertEqual(output.read_bytes(), b"valid-video")


if __name__ == "__main__":
    unittest.main()
