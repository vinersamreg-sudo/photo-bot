from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw

from app.content_studio.before_after_renderer import TEMPLATES
from app.content_studio.cli import build_parser
from app.content_studio.config import ContentStudioSettings
from app.content_studio.content_generator import CTA_LEAD, DISCLOSURE, ContentGenerator
from app.content_studio.models import (
    ContentCategory,
    LicenseStatus,
    PostStatus,
    PublicationMode,
    QualityIssue,
    TransformationType,
)
from app.content_studio.publisher import MaxPublisher, PublishingDisabledError
from app.content_studio.service import ContentStudioService


class FakeTransport:
    def __init__(self) -> None:
        self.published = 0
        self.retried = 0

    def publish(self, payload):
        self.published += 1
        return "max-post-1"

    def retry(self, payload, previous_external_id):
        self.retried += 1
        return "max-post-2"


class ContentStudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.approved = self.root / "marketing" / "assets" / "approved"
        self.approved.mkdir(parents=True)
        self.settings = ContentStudioSettings(
            base_dir=self.root / "content",
            database_path=self.root / "content" / "content.sqlite3",
            storage_dir=self.root / "content" / "storage",
            approved_assets_dir=self.approved,
            publishing_enabled=False,
            bot_url="https://max.ru/ravuna_bot",
        )
        self.before = self.approved / "before.png"
        self.after = self.approved / "after.png"
        _image(self.before, "#DFB18D", "#17355A", 90)
        _image(self.after, "#DCE9F5", "#2C6947", 210)
        self.service = ContentStudioService(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_asset(self):
        return self.service.add_asset(
            self.before,
            title="Демонстрационный портрет",
            description="Создан специально для Ravuna",
            category=ContentCategory.REPLACE_BACKGROUND,
            tags=("портрет", "фон", "портрет"),
        )

    def generate(self, **overrides):
        asset = overrides.pop("asset", None) or self.add_asset()
        values = {
            "asset_id": asset.id,
            "after_image": self.after,
            "transformation_type": TransformationType.REPLACE_BACKGROUND,
            "prompt_en": "Replace the background with a bright modern studio.",
        }
        values.update(overrides)
        return self.service.generate(**values)

    def test_schema_is_separate_from_user_gallery_and_payment_database(self) -> None:
        connection = sqlite3.connect(self.settings.database_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertIn("demo_assets", tables)
        self.assertIn("demo_posts", tables)
        self.assertNotIn("users", tables)
        self.assertNotIn("gallery_versions", tables)
        self.assertNotIn("payment_orders", tables)

    def test_pre_release_database_is_migrated_without_losing_existing_data(self) -> None:
        legacy_path = self.root / "legacy.sqlite3"
        from app.content_studio.repository import ContentStudioRepository

        repository = ContentStudioRepository(legacy_path)
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute("ALTER TABLE demo_posts DROP COLUMN generator_prompt_en")
            connection.execute("CREATE TABLE migration_sentinel(value TEXT NOT NULL)")
            connection.execute("INSERT INTO migration_sentinel(value) VALUES('preserved')")
            connection.execute("DELETE FROM content_schema_migrations WHERE version=2")
            connection.commit()
        finally:
            connection.close()

        repository = ContentStudioRepository(legacy_path)
        connection = repository.connect()
        try:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(demo_posts)")
            }
            preserved = connection.execute("SELECT value FROM migration_sentinel").fetchone()
            migration = connection.execute(
                "SELECT name FROM content_schema_migrations WHERE version=2"
            ).fetchone()
        finally:
            connection.close()
        self.assertIn("generator_prompt_en", columns)
        self.assertEqual(preserved[0], "preserved")
        self.assertEqual(migration[0], "post_generator_prompt_metadata")

    def test_status_starts_safe_and_empty(self) -> None:
        status = self.service.status()
        self.assertEqual(status["quick_check"], "ok")
        self.assertEqual(status["published_posts"], 0)
        self.assertFalse(status["publishing_enabled"])
        self.assertEqual(status["external_ai_requests"], 0)

    def test_asset_requires_verified_commercial_ravuna_rights(self) -> None:
        with self.assertRaisesRegex(ValueError, "verified commercial"):
            self.service.add_asset(
                self.before,
                title="Unsafe",
                description="",
                category=ContentCategory.PORTRAIT,
                license_status=LicenseStatus.PENDING,
            )

    def test_asset_checksum_is_unique_and_failed_duplicate_leaves_no_row(self) -> None:
        first = self.add_asset()
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_asset()
        self.assertEqual(self.service.status()["assets"], 1)
        self.assertEqual(len(first.checksum), 64)

    def test_storage_rejects_path_escape(self) -> None:
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.service.storage.resolve("../../outside.png")

    def test_generate_builds_atomic_bundle_and_three_cards(self) -> None:
        generated = self.generate()
        self.assertEqual(generated["status"], "needs_review")
        self.assertEqual(generated["cards"], ["square", "stories", "vertical"])
        self.assertEqual(generated["external_ai_requests"], 0)
        self.assertEqual(generated["external_publications"], 0)
        status = self.service.status()
        self.assertEqual(status["assets"], 1)
        self.assertEqual(status["transformations"], 1)
        self.assertEqual(status["results"], 1)
        self.assertEqual(status["posts"], 1)

    def test_renderer_dimensions_match_all_templates(self) -> None:
        generated = self.generate()
        result = self.service.repository.get_result(generated["result_id"])
        plan = json.loads(result["edit_plan_json"])
        for name, spec in TEMPLATES.items():
            with Image.open(self.service.storage.resolve(plan["content_cards"][name])) as card:
                self.assertEqual(card.size, (spec.width, spec.height))

    def test_generated_post_is_factual_disclosed_and_utm_tagged(self) -> None:
        generated = self.generate()
        post = self.service.repository.get_post(generated["post_id"])
        self.assertIn(DISCLOSURE, post["body"])
        self.assertIn(CTA_LEAD, post["cta"])
        self.assertIn("utm_source=max", post["utm_url"])
        self.assertIn("utm_medium=channel", post["utm_url"])
        self.assertIn("utm_campaign=demo_posts", post["utm_url"])
        self.assertNotIn("к нам обрати", post["body"].lower())
        self.assertTrue(post["generator_prompt_en"].isascii())

    def test_all_templates_include_mandatory_marking(self) -> None:
        generator = ContentGenerator("https://max.ru/ravuna_bot")
        for transformation in TransformationType:
            with self.subTest(transformation=transformation.value):
                copy = generator.generate(transformation, post_id="post", platform="max")
                self.assertIn("Демонстрационный пример Ravuna.", copy.body)
                self.assertIn("Изображения созданы специально", copy.body)
                self.assertIn("2 бесплатные обработки", copy.cta)

    def test_non_english_transformation_prompt_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "English"):
            self.generate(prompt_en="Замени фон")

    def test_platform_neutral_generation_builds_platform_utm(self) -> None:
        generated = self.generate(platform="telegram")
        post = self.service.repository.get_post(generated["post_id"])
        self.assertEqual(post["platform"], "telegram")
        self.assertIn("utm_source=telegram", post["utm_url"])

    def test_quality_issue_forces_review_and_blocks_approval(self) -> None:
        generated = self.generate(reported_issues=(QualityIssue.SIX_FINGERS,))
        self.assertEqual(generated["quality_issues"], ["six_fingers"])
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.service.approve(
                generated["post_id"], reviewer="owner", reason="visual check", apply=True
            )
        post = self.service.repository.get_post(generated["post_id"])
        self.assertEqual(post["publish_status"], PostStatus.NEEDS_REVIEW.value)

    def test_unchanged_result_is_detected(self) -> None:
        generated = self.generate(after_image=self.before)
        self.assertIn("unchanged_result", generated["quality_issues"])

    def test_approve_is_dry_run_by_default_and_apply_is_audited(self) -> None:
        generated = self.generate()
        preview = self.service.approve(
            generated["post_id"], reviewer="owner", reason="checked", apply=False
        )
        self.assertFalse(preview["apply"])
        self.assertEqual(
            self.service.repository.get_post(generated["post_id"])["publish_status"],
            PostStatus.NEEDS_REVIEW.value,
        )
        self.service.approve(
            generated["post_id"], reviewer="owner", reason="checked", apply=True
        )
        self.assertEqual(
            self.service.repository.get_post(generated["post_id"])["publish_status"],
            PostStatus.APPROVED.value,
        )
        result_id = self.service.repository.get_post(generated["post_id"])["result_id"]
        self.assertEqual(
            self.service.repository.get_result(result_id)["status"],
            "approved",
        )

    def test_schedule_requires_approved_post_and_timezone(self) -> None:
        generated = self.generate()
        with self.assertRaisesRegex(ValueError, "approved"):
            self.service.schedule(generated["post_id"], "2026-07-22T09:00:00+04:00")
        self.service.approve(
            generated["post_id"], reviewer="owner", reason="checked", apply=True
        )
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.service.schedule(generated["post_id"], "2026-07-22T09:00:00")
        result = self.service.schedule(
            generated["post_id"], "2026-07-22T09:00:00+04:00", apply=True
        )
        self.assertTrue(result["apply"])

    def test_weekly_plan_is_deterministic_and_dry_run_first(self) -> None:
        preview = self.service.create_plan(date(2026, 7, 20), 7, apply=False)
        self.assertEqual(preview["created"], 0)
        self.assertEqual(preview["entries"][0]["content_type"], "before_after")
        self.assertEqual(preview["entries"][6]["content_type"], "restoration")
        applied = self.service.create_plan(date(2026, 7, 20), 7, apply=True)
        self.assertEqual(applied["created"], 7)

    def test_preview_and_dry_run_never_publish(self) -> None:
        generated = self.generate()
        preview = self.service.publication(
            generated["post_id"], mode=PublicationMode.PREVIEW
        )
        dry_run = self.service.publication(
            generated["post_id"], mode=PublicationMode.DRY_RUN
        )
        self.assertEqual(preview.status, "planned")
        self.assertEqual(dry_run.status, "simulated")
        self.assertTrue(dry_run.payload["demo_disclosure_present"])
        self.assertEqual(dry_run.payload["media_path"], "<content-studio-media>")
        self.assertEqual(self.service.status()["publication_attempts"], 2)
        self.assertEqual(self.service.status()["published_posts"], 0)

    def test_network_publish_fails_closed_without_flag(self) -> None:
        generated = self.generate()
        self.service.approve(
            generated["post_id"], reviewer="owner", reason="checked", apply=True
        )
        with self.assertRaises(PublishingDisabledError):
            self.service.publication(
                generated["post_id"], mode=PublicationMode.PUBLISH, apply=True
            )
        self.assertEqual(self.service.status()["published_posts"], 0)
        self.assertEqual(self.service.status()["publication_attempts"], 1)

    def test_publish_due_is_bounded_reviewed_and_dry_run_first(self) -> None:
        generated = self.generate()
        self.service.approve(
            generated["post_id"], reviewer="owner", reason="checked", apply=True
        )
        self.service.schedule(
            generated["post_id"], "2020-01-01T00:00:00+00:00", apply=True
        )
        preview = self.service.publish_due(limit=1, apply=False)
        self.assertEqual(preview["due"], [generated["post_id"]])
        self.assertEqual(preview["published"], [])

        transport = FakeTransport()
        self.service.publishers["max"] = MaxPublisher(
            publishing_enabled=True, transport=transport
        )
        result = self.service.publish_due(limit=1, apply=True)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["published"][0]["post_id"], generated["post_id"])
        self.assertEqual(transport.published, 1)

    def test_publisher_interface_supports_publish_and_retry_with_injected_transport(self) -> None:
        transport = FakeTransport()
        publisher = MaxPublisher(publishing_enabled=True, transport=transport)
        post = {
            "id": "post",
            "title": "Demo",
            "body": DISCLOSURE,
            "cta": "CTA",
            "hashtags_json": "[]",
            "utm_url": "https://max.ru/bot?utm_source=max",
            "published_external_id": None,
        }
        first = publisher.publish(post, "cards/card.png")
        second = publisher.retry(post, "cards/card.png")
        self.assertEqual(first.external_id, "max-post-1")
        self.assertEqual(second.external_id, "max-post-2")
        self.assertEqual((transport.published, transport.retried), (1, 1))

    def test_manual_publish_and_analytics_are_explicit(self) -> None:
        generated = self.generate()
        self.service.approve(
            generated["post_id"], reviewer="owner", reason="checked", apply=True
        )
        outcome = self.service.publication(
            generated["post_id"],
            mode=PublicationMode.MANUAL_PUBLISH,
            external_id="manual-max-1",
            apply=True,
        )
        self.assertEqual(outcome.external_id, "manual-max-1")
        summary = self.service.record_analytics(
            generated["post_id"],
            views=100,
            clicks=10,
            reactions=4,
            comments=2,
            conversion_to_bot=3,
            starts=8,
            first_photos=6,
            generations=5,
            payments=1,
        )
        self.assertEqual(summary["ctr"], 0.1)
        self.assertEqual(summary["conversions"], 3)
        self.assertEqual(
            (
                summary["starts"],
                summary["first_photos"],
                summary["generations"],
                summary["payments"],
            ),
            (8, 6, 5, 1),
        )
        latest = self.service.record_analytics(
            generated["post_id"],
            views=140,
            clicks=14,
            reactions=7,
            comments=3,
            conversion_to_bot=4,
        )
        self.assertEqual(latest["snapshots"], 2)
        self.assertEqual(latest["views"], 140)
        self.assertEqual(latest["clicks"], 14)
        self.assertEqual(latest["conversions"], 4)

    def test_queue_filters_lifecycle_state(self) -> None:
        generated = self.generate()
        queue = self.service.repository.queue(PostStatus.NEEDS_REVIEW.value)
        self.assertEqual([post["id"] for post in queue], [generated["post_id"]])
        self.assertEqual(self.service.repository.queue(PostStatus.PUBLISHED.value), [])

    def test_cli_exposes_requested_commands(self) -> None:
        parser = build_parser()
        for command in (
            "status", "auto-run", "dashboard", "permissions", "generate", "queue",
            "approve", "publish", "publish-due", "analytics", "schedule", "video",
        ):
            with self.subTest(command=command):
                if command in {"status", "auto-run", "dashboard", "permissions"}:
                    args = parser.parse_args(["content", command])
                elif command in {"queue", "analytics", "publish-due"}:
                    args = parser.parse_args(["content", command])
                elif command == "approve":
                    args = parser.parse_args(
                        ["content", command, "--post-id", "x", "--reviewer", "r", "--reason", "ok"]
                    )
                elif command == "publish":
                    args = parser.parse_args(["content", command, "--post-id", "x"])
                elif command == "schedule":
                    args = parser.parse_args(["content", command, "--plan-start", "2026-07-20"])
                elif command == "video":
                    args = parser.parse_args(
                        [
                            "content", command,
                            "--before", "before.png",
                            "--after", "after.png",
                            "--output", "video.mp4",
                            "--hook", "Hook",
                        ]
                    )
                else:
                    args = parser.parse_args(
                        [
                            "content",
                            command,
                            "--asset-id",
                            "x",
                            "--after-image",
                            "x.png",
                            "--transformation",
                            "replace_background",
                            "--prompt-en",
                            "Replace background",
                        ]
                    )
                self.assertEqual(args.content_command, command)

    def test_optional_timer_runs_only_bounded_due_publication(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        service = (project_root / "ops" / "ravuna-content-publisher.service").read_text(
            encoding="utf-8"
        )
        timer = (project_root / "ops" / "ravuna-content-publisher.timer").read_text(
            encoding="utf-8"
        )
        deploy = (project_root / "ops" / "deploy_ravuna_content_studio.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("content auto-run --apply", service)
        self.assertIn("WorkingDirectory=/opt/ravuna-content/current", service)
        self.assertIn("ReadWritePaths=/opt/ravuna-content/data", service)
        self.assertIn("ReadOnlyPaths=/opt/photo-bot/data", service)
        self.assertIn("TimeoutStartSec=20min", service)
        self.assertNotIn("photo-bot.service", service)
        self.assertNotIn("restart", service.lower())
        self.assertIn("OnCalendar=*:0/15", timer)
        self.assertIn("/opt/ravuna-content", deploy)
        self.assertNotIn("systemctl restart photo-bot", deploy)
        self.assertNotIn("/opt/photo-bot/.env", deploy)

    def test_category_catalog_contains_every_required_category(self) -> None:
        required = {
            "replace_background", "remove_object", "remove_person", "portrait",
            "business_photo", "old_photo", "restore", "upscale", "colorize",
            "replace_clothes", "travel", "car", "real_estate", "products",
            "family", "memorial_restoration", "document_photo", "avatar",
            "wedding", "nature",
        }
        self.assertEqual({category.value for category in ContentCategory}, required)


def _image(path: Path, background: str, foreground: str, offset: int) -> None:
    image = Image.new("RGB", (640, 640), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((offset, 70, offset + 240, 560), fill=foreground)
    draw.ellipse((260, 180, 520, 440), fill="#F0C95C")
    image.save(path, format="PNG")


if __name__ == "__main__":
    unittest.main()
