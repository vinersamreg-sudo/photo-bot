from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from app.content_studio.config import ContentStudioSettings
from app.content_studio.content_generator import ContentGenerator
from app.content_studio.growth import FullAutoGrowthEngine
from app.content_studio.models import (
    ContentCategory,
    PublicationMode,
    TransformationType,
)
from app.content_studio.novelty import (
    DuplicateContentError,
    NoveltyFingerprint,
    TEXT_SIMILARITY_THRESHOLD,
    evaluate_fingerprint,
    normalize_caption,
    normalized_text_hash,
    perceptual_hash,
    phash_distance,
    text_similarity,
)
from app.content_studio.publisher import MaxPublisher
from app.content_studio.service import ContentStudioService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CountingTransport:
    def __init__(self) -> None:
        self.published = 0
        self.retried = 0

    def publish(self, payload):
        self.published += 1
        return f"max-{self.published}"

    def retry(self, payload, previous_external_id):
        self.retried += 1
        return f"max-retry-{self.retried}"


class ContentStudioNoveltyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_same_asset_with_new_uuid_is_blocked_before_second_max_call(self) -> None:
        approved = self.root / "approved"
        approved.mkdir()
        before = approved / "before.png"
        after = approved / "after.png"
        _pattern(before, "#eee1ce", "#24506f", 70)
        _pattern(after, "#d4e8ef", "#355f3c", 160)
        transport = CountingTransport()
        settings = _settings(self.root, approved, publishing=True)
        service = ContentStudioService(
            settings,
            publisher=MaxPublisher(publishing_enabled=True, transport=transport),
        )
        asset = service.add_asset(
            before,
            title="Портрет",
            description="Synthetic",
            category=ContentCategory.PORTRAIT,
        )
        posts = []
        for _ in range(2):
            generated = service.generate(
                asset_id=asset.id,
                after_image=after,
                transformation_type=TransformationType.PORTRAIT,
                prompt_en="Create a factual synthetic portrait demonstration.",
            )
            service.approve(
                generated["post_id"], reviewer="owner", reason="synthetic", apply=True
            )
            posts.append(generated["post_id"])

        service.publication(posts[0], mode=PublicationMode.PUBLISH, apply=True)
        connection = service.repository.connect()
        try:
            connection.execute(
                "DELETE FROM content_publication_history WHERE post_id=?", (posts[0],)
            )
        finally:
            connection.close()
        self.assertEqual(service.novelty.ensure_published_history(), 1)
        self.assertEqual(service.novelty.ensure_published_history(), 0)
        with self.assertRaisesRegex(DuplicateContentError, "asset_checksum_reused"):
            service.publication(posts[1], mode=PublicationMode.PUBLISH, apply=True)

        self.assertEqual(transport.published, 1)
        self.assertEqual(service.repository.get_post(posts[1])["publish_status"], "archived")
        self.assertEqual(service.status()["publication_history"], 1)

    def test_resize_and_reencode_is_a_visual_near_duplicate(self) -> None:
        original = self.root / "original.png"
        reencoded = self.root / "reencoded.jpg"
        _pattern(original, "#e2d5c8", "#2c567c", 90)
        with Image.open(original) as image:
            image.resize((480, 480), Image.Resampling.LANCZOS).save(
                reencoded, "JPEG", quality=84
            )
        first_hash = perceptual_hash(original)
        second_hash = perceptual_hash(reencoded)
        self.assertLessEqual(phash_distance(first_hash, second_hash), 6)

        previous = _fingerprint(
            post_id="old", asset_checksum="a" * 64, card_phash=first_hash
        )
        candidate = _fingerprint(
            post_id="new", asset_checksum="b" * 64, card_phash=second_hash
        )
        decision = evaluate_fingerprint(candidate, [previous.record()])
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "visual_near_duplicate")

    def test_caption_normalization_ignores_new_utm_uuid_hashtags_and_disclosure(self) -> None:
        first = normalize_caption(
            "Аккуратный фон",
            "Показываем результат. https://max.ru/bot?utm_content=old #Ravuna "
            "Демонстрационный пример Ravuna.\n"
            "Изображения созданы специально для демонстрации возможностей сервиса.",
        )
        second = normalize_caption(
            "Аккуратный фон",
            "Показываем результат. https://max.ru/bot?utm_content=new "
            "#ИИФото 7b0dc51c-b8cc-4ff5-8b75-47164e85aa82",
        )
        self.assertEqual(first, second)
        self.assertEqual(normalized_text_hash(first), normalized_text_hash(second))
        previous = _fingerprint(post_id="old-utm", normalized_text=first)
        candidate = _fingerprint(
            post_id="new-utm",
            asset_checksum="b" * 64,
            card_phash="f" * 16,
            normalized_text=second,
            scheduled_time="2026-08-24T15:00:00+00:00",
        )
        self.assertEqual(
            evaluate_fingerprint(candidate, [previous.record()]).reason,
            "text_exact_duplicate",
        )

    def test_slightly_rephrased_caption_is_blocked_at_point_eighty_two(self) -> None:
        first = normalize_caption(
            "Замена фона",
            "Пример запроса: улучшить фон, сохранив человека. Показываем результат до и после.",
        )
        second = normalize_caption(
            "Замена фона",
            "Пример запроса: улучшить фон и сохранить человека. Показываем результат до и после.",
        )
        self.assertGreaterEqual(text_similarity(first, second), TEXT_SIMILARITY_THRESHOLD)
        previous = _fingerprint(post_id="old", normalized_text=first)
        candidate = _fingerprint(
            post_id="new",
            asset_checksum="b" * 64,
            card_phash="f" * 16,
            normalized_text=second,
        )
        decision = evaluate_fingerprint(candidate, [previous.record()])
        self.assertEqual(decision.reason, "text_near_duplicate")

    def test_different_photo_and_copy_pass(self) -> None:
        previous = _fingerprint(post_id="old")
        candidate = _fingerprint(
            post_id="new",
            asset_checksum="b" * 64,
            card_phash="f" * 16,
            normalized_text="восстановление старого семейного снимка цвет и детали",
            category="restore",
            transformation_type="restore_photo",
            scheduled_time="2026-08-24T15:00:00+00:00",
        )
        self.assertTrue(evaluate_fingerprint(candidate, [previous.record()]).allowed)

    def test_third_recent_category_reselects_when_alternative_exists(self) -> None:
        history = [
            _fingerprint(
                post_id=f"old-{index}",
                asset_checksum=f"{index + 1:064x}",
                card_phash=f"{index + 1:016x}",
                normalized_text=f"уникальная демонстрация номер {index}",
                scheduled_time=f"2026-08-{20 + index:02d}T15:00:00+00:00",
            ).record()
            for index in range(3)
        ]
        candidate = _fingerprint(
            post_id="new",
            asset_checksum="f" * 64,
            card_phash="f" * 16,
            normalized_text="совершенно новая демонстрация портрета",
        )
        decision = evaluate_fingerprint(
            candidate, history, alternative_categories={"portrait", "restore"}
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "recent_category_saturation")

    def test_demo_copy_varies_hook_task_and_result_without_customer_stories(self) -> None:
        generator = ContentGenerator("https://max.ru/ravuna_bot")
        copies = [
            generator.generate_demo_case(
                TransformationType.REPLACE_BACKGROUND,
                post_id=f"demo-{index}",
                platform="max",
                title="Аккуратный фон",
                hook="Один запрос — и фон больше не мешает",
                prompt_example="Улучши фон, сохрани человека",
                hashtags=("#Ravuna",),
            )
            for index in range(20)
        ]
        self.assertEqual(len({copy.title for copy in copies}), 3)
        self.assertEqual(len({copy.body.split(":", 1)[0] for copy in copies}), 3)
        self.assertEqual(
            len(
                {
                    copy.body.split("\n\n", 2)[1]
                    for copy in copies
                }
            ),
            3,
        )
        combined = "\n".join(copy.body.casefold() for copy in copies)
        self.assertNotIn("наш клиент", combined)
        self.assertNotIn("к нам обрати", combined)

    def test_exhausted_library_skips_without_max_call(self) -> None:
        transport = CountingTransport()
        settings = ContentStudioSettings(
            base_dir=self.root / "content",
            database_path=self.root / "content" / "content.sqlite3",
            storage_dir=self.root / "content" / "storage",
            approved_assets_dir=PROJECT_ROOT / "marketing/assets/approved",
            publishing_enabled=True,
            full_auto_enabled=True,
            content_library_path=PROJECT_ROOT / "marketing/content/library.json",
            production_database_path=self.root / "missing.sqlite3",
        )
        service = ContentStudioService(
            settings,
            publisher=MaxPublisher(publishing_enabled=True, transport=transport),
        )
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
        }
        result = engine.maintain_queue(
            now=datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc), apply=True
        )
        self.assertTrue(
            any(item["error"] == "candidate_pool_exhausted" for item in result["skipped"])
        )
        self.assertEqual(transport.published, 0)
        self.assertGreater(service.status()["novelty_events"], 0)

    def test_retry_of_published_job_never_calls_transport_again(self) -> None:
        approved = self.root / "retry-approved"
        approved.mkdir()
        before = approved / "before.png"
        after = approved / "after.png"
        _pattern(before, "#f0dfca", "#2a4e72", 65)
        _pattern(after, "#d5eaf2", "#38623d", 170)
        transport = CountingTransport()
        service = ContentStudioService(
            _settings(self.root / "retry", approved, publishing=True),
            publisher=MaxPublisher(publishing_enabled=True, transport=transport),
        )
        asset = service.add_asset(
            before,
            title="Портрет",
            description="Synthetic",
            category=ContentCategory.PORTRAIT,
        )
        generated = service.generate(
            asset_id=asset.id,
            after_image=after,
            transformation_type=TransformationType.PORTRAIT,
            prompt_en="Create a factual synthetic portrait demonstration.",
        )
        service.approve(
            generated["post_id"], reviewer="owner", reason="synthetic", apply=True
        )
        service.publication(
            generated["post_id"], mode=PublicationMode.PUBLISH, apply=True
        )
        with self.assertRaisesRegex(ValueError, "approved or scheduled"):
            service.publication(
                generated["post_id"], mode=PublicationMode.RETRY, apply=True
            )
        self.assertEqual((transport.published, transport.retried), (1, 0))


def _fingerprint(
    *,
    post_id: str,
    asset_checksum: str = "a" * 64,
    card_phash: str = "0" * 16,
    normalized_text: str = "новый портрет с аккуратным студийным фоном",
    category: str = "portrait",
    transformation_type: str = "portrait",
    scheduled_time: str = "2026-08-23T15:00:00+00:00",
) -> NoveltyFingerprint:
    return NoveltyFingerprint(
        post_id=post_id,
        platform="max",
        asset_id=f"asset-{post_id}",
        asset_checksum=asset_checksum,
        transformation_type=transformation_type,
        category=category,
        before_phash=card_phash,
        after_phash=card_phash,
        card_phash=card_phash,
        normalized_text_hash=normalized_text_hash(normalized_text),
        normalized_text=normalized_text,
        scheduled_time=scheduled_time,
    )


def _settings(root: Path, approved: Path, *, publishing: bool) -> ContentStudioSettings:
    return ContentStudioSettings(
        base_dir=root / "content",
        database_path=root / "content" / "content.sqlite3",
        storage_dir=root / "content" / "storage",
        approved_assets_dir=approved,
        publishing_enabled=publishing,
        bot_url="https://max.ru/ravuna_bot",
    )


def _pattern(path: Path, background: str, foreground: str, offset: int) -> None:
    image = Image.new("RGB", (640, 640), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((offset, 60, offset + 250, 550), fill=foreground)
    draw.ellipse((250, 170, 520, 450), fill="#eebc55")
    image.save(path, "PNG")


if __name__ == "__main__":
    unittest.main()

