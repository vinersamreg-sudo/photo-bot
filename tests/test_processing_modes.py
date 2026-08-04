from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image, ImageDraw

from app.background_assets import AssetPolicyError, BackgroundCatalog
from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
from app.domain import AssetUnavailableError, SegmentationFailedError
from app.edit_intent import EditPlan, merge_edit_plans, parse_edit_intent, repeat_edit_plan
from app.image_provider import FakeImageProvider
from app.processing_modes import ProcessingMode, ProcessingPlan
from app.processing_pipeline import HybridProcessingExecutor, cleanup_processing_temp
from app.processing_router import ModeRouter
from app.prompt_builder import build_provider_prompt
from app.segmentation import FixedMaskSegmenter
from app.storage import PrivateStorage
from app.watermark import WatermarkService


class ProcessingModeTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.asset_root = self.root / "assets"
        self.asset_root.mkdir()
        self.background_path = self.asset_root / "rocky-owned.png"
        background = Image.new("RGB", (600, 900), "#88a7bd")
        draw = ImageDraw.Draw(background)
        draw.polygon([(0, 620), (180, 220), (330, 620)], fill="#5d625d")
        draw.polygon([(180, 620), (410, 170), (600, 620)], fill="#747a72")
        background.save(self.background_path)
        self.asset_checksum = hashlib.sha256(self.background_path.read_bytes()).hexdigest()
        self.manifest_path = self.asset_root / "catalog.json"
        self._write_manifest([self._asset_payload()])
        self.catalog = BackgroundCatalog.load(self.manifest_path)
        self.provider = FakeImageProvider()
        self.router = ModeRouter(
            self.catalog,
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            real_background_enabled=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _asset_payload(self, **overrides):
        value = {
            "id": "rocky_mountains_owned_01",
            "title": "Owned rocky mountain fixture",
            "category": "rocky_mountains",
            "tags": ["rocks", "daylight"],
            "location_type": "natural_landscape",
            "orientation": "portrait",
            "aspect_ratio": 600 / 900,
            "dominant_lighting": "front_soft_daylight",
            "time_of_day": "day",
            "weather": "clear",
            "horizon_position": 0.48,
            "source_type": "pixora_owned",
            "source_reference": "internal-test-record",
            "license_type": "pixora_owned",
            "license_record": "tests/fixtures/owner-declaration-v1",
            "commercial_use_allowed": True,
            "attribution_required": False,
            "file_checksum": self.asset_checksum,
            "file_name": self.background_path.name,
            "active": True,
            "created_at": "2026-07-16T00:00:00+00:00",
        }
        value.update(overrides)
        return value

    def _write_manifest(self, assets) -> None:
        self.manifest_path.write_text(
            json.dumps({"schema_version": 1, "assets": assets}), encoding="utf-8"
        )

    def _source_and_mask(self) -> tuple[Path, Image.Image]:
        source_path = self.root / "source.png"
        source = Image.new("RGB", (400, 600), "#d7d2cc")
        draw = ImageDraw.Draw(source)
        draw.ellipse((115, 55, 285, 225), fill="#e4b69a")
        draw.rounded_rectangle((80, 190, 320, 590), radius=70, fill="#e8e6df")
        source.save(source_path)
        mask = Image.new("L", source.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((108, 48, 292, 235), fill=255)
        mask_draw.rounded_rectangle((72, 180, 328, 600), radius=75, fill=255)
        return source_path, mask

    def test_router_selects_requested_modes(self) -> None:
        cases = {
            "Замени фон на реалистичные скалистые горы": ProcessingMode.REAL_BACKGROUND_COMPOSITE,
            "Фэнтезийные парящие горы в магическом мире": ProcessingMode.AI_GENERATION,
            "Поменяй куртку": ProcessingMode.LOCAL_AI_EDIT,
            "Улучши качество": ProcessingMode.ENHANCEMENT,
            "Восстанови старое фото и убери царапины": ProcessingMode.RESTORATION,
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                plan = self.router.route(
                    parse_edit_intent(phrase),
                    source_orientation="portrait",
                    source_aspect_ratio=2 / 3,
                )
                self.assertEqual(plan.selected_mode, expected)

    def test_real_background_requires_asset_and_never_silently_falls_back(self) -> None:
        empty = ModeRouter(
            BackgroundCatalog.empty(self.root / "empty.json"),
            provider_name="openai",
            provider_model="gpt-image-2",
            real_background_enabled=True,
        )
        result = empty.route(parse_edit_intent("Замени фон на реалистичные скалистые горы"))
        self.assertEqual(result.selected_mode, ProcessingMode.REAL_BACKGROUND_COMPOSITE)
        self.assertEqual(result.fallback_mode, ProcessingMode.AI_GENERATION)
        self.assertTrue(result.requires_user_confirmation)
        self.assertIsNone(result.asset_id)

    def test_explicit_ai_background_can_select_generation(self) -> None:
        result = self.router.route(
            parse_edit_intent("Создай AI-фон с реалистичными скалистыми горами")
        )
        self.assertEqual(result.selected_mode, ProcessingMode.AI_GENERATION)

    def test_correction_and_repeat_preserve_asset_lineage(self) -> None:
        first_edit = parse_edit_intent("Замени фон на реалистичные скалистые горы")
        first_processing = self.router.route(
            first_edit, source_orientation="portrait", source_aspect_ratio=2 / 3
        )
        correction = merge_edit_plans(
            first_edit,
            parse_edit_intent(
                "Оставь эти же горы, сделай их резче", mode="correction"
            ),
        )
        correction_processing = self.router.route(correction, parent=first_processing)
        self.assertEqual(correction_processing.selected_mode, ProcessingMode.ENHANCEMENT)
        self.assertEqual(correction_processing.asset_id, first_processing.asset_id)
        repeated = self.router.route(
            repeat_edit_plan(correction), parent=correction_processing
        )
        self.assertEqual(repeated.asset_id, first_processing.asset_id)
        self.assertEqual(repeated.selected_mode, ProcessingMode.ENHANCEMENT)

    def test_local_correction_keeps_parent_asset_but_does_not_reselect_it(self) -> None:
        first = self.router.route(
            parse_edit_intent("Замени фон на реалистичные скалистые горы"),
            source_orientation="portrait",
            source_aspect_ratio=2 / 3,
        )
        local = self.router.route(parse_edit_intent("Поменяй куртку"), parent=first)
        self.assertEqual(local.selected_mode, ProcessingMode.LOCAL_AI_EDIT)
        self.assertEqual(local.asset_id, first.asset_id)

    def test_asset_license_checksum_and_path_policy_fail_closed(self) -> None:
        bad_cases = (
            {"commercial_use_allowed": False},
            {"license_record": ""},
            {"license_type": "unknown"},
            {"file_checksum": "0" * 64},
            {"file_name": "../outside.png"},
        )
        for changes in bad_cases:
            with self.subTest(changes=changes):
                self._write_manifest([self._asset_payload(**changes)])
                with self.assertRaises(AssetPolicyError):
                    BackgroundCatalog.load(self.manifest_path)

    def test_composite_preserves_geometry_and_uses_no_openai_call(self) -> None:
        source_path, mask = self._source_and_mask()
        executor = HybridProcessingExecutor(
            self.provider,
            self.catalog,
            FixedMaskSegmenter(mask),
            self.root / "temp",
        )
        plan = self.router.route(
            parse_edit_intent("Замени фон на реалистичные скалистые горы"),
            source_orientation="portrait",
            source_aspect_ratio=2 / 3,
        )
        result = executor.execute(source_path, "English prompt only.", plan)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(result.estimated_cost_rub, 0)
        output_path = self.root / "result.png"
        output_path.write_bytes(result.image_bytes)
        with Image.open(output_path) as output:
            self.assertEqual(output.size, (400, 600))
            self.assertEqual(output.mode, "RGB")
        self.assertEqual(list((self.root / "temp").glob("pixora-mask-*")), [])

    def _service(self, segmenter: FixedMaskSegmenter) -> tuple[DemoService, Path]:
        source_path, _mask = self._source_and_mask()
        settings = Settings(
            openai_api_key="test",
            openai_image_model="fake",
            app_env="test",
            base_dir=self.root,
            demo_min_request_interval_seconds=1,
            image_direct_prompt_enabled=False,
        )
        storage = PrivateStorage(settings.users_dir, 10 * 1024 * 1024)
        executor = HybridProcessingExecutor(
            self.provider, self.catalog, segmenter, settings.temp_dir
        )
        service = DemoService(
            settings,
            Database(settings.database_path),
            storage,
            WatermarkService("ОБРАЗЕЦ", 1024, "JPEG", 82),
            self.provider,
            processing_router=self.router,
            processing_executor=executor,
        )
        return service, source_path

    def test_processing_plan_persists_and_quota_charges_after_delivery(self) -> None:
        source_path, mask = self._source_and_mask()
        service, _ = self._service(FixedMaskSegmenter(mask))
        session = service.start_session("max", "owner", source_path)
        result = service.generate(
            session.session_id,
            "Замени фон на реалистичные скалистые горы",
            "mode-persistence",
        )
        self.assertEqual(result.remaining_generations, 1)
        with service.database.read() as connection:
            attempt = connection.execute(
                "SELECT * FROM generation_attempts WHERE id=?", (result.attempt_id,)
            ).fetchone()
            version = connection.execute(
                "SELECT * FROM gallery_versions WHERE attempt_id=?", (result.attempt_id,)
            ).fetchone()
        self.assertEqual(attempt["selected_mode"], "REAL_BACKGROUND_COMPOSITE")
        self.assertEqual(attempt["asset_id"], "rocky_mountains_owned_01")
        self.assertEqual(len(attempt["asset_checksum"]), 64)
        self.assertEqual(version["asset_checksum"], attempt["asset_checksum"])
        self.assertEqual(version["processing_plan_json"], attempt["processing_plan_json"])
        self.assertEqual(self.provider.calls, 0)

    def test_segmentation_failure_has_no_quota_or_gallery_version(self) -> None:
        source_path, mask = self._source_and_mask()
        service, _ = self._service(FixedMaskSegmenter(mask, fail=True))
        session = service.start_session("max", "owner-fail", source_path)
        with self.assertRaises(SegmentationFailedError):
            service.generate(
                session.session_id,
                "Замени фон на реалистичные скалистые горы",
                "segmentation-failure",
            )
        with service.database.read() as connection:
            refreshed = connection.execute(
                "SELECT * FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()
            versions = connection.execute(
                "SELECT COUNT(*) FROM gallery_versions WHERE gallery_item_id=?",
                (refreshed["gallery_item_id"],),
            ).fetchone()[0]
            attempt = connection.execute(
                "SELECT * FROM generation_attempts WHERE idempotency_key='segmentation-failure'"
            ).fetchone()
        self.assertEqual(refreshed["successful_generations"], 0)
        self.assertEqual(versions, 0)
        self.assertEqual(attempt["status"], "failed_technical")

    def test_empty_asset_fails_before_attempt_and_provider(self) -> None:
        source_path, mask = self._source_and_mask()
        empty_router = ModeRouter(
            BackgroundCatalog.empty(self.root / "empty.json"),
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            real_background_enabled=True,
        )
        settings = Settings(
            "test", "fake", "test", self.root,
            image_direct_prompt_enabled=False,
        )
        service = DemoService(
            settings,
            Database(settings.database_path),
            PrivateStorage(settings.users_dir, 10 * 1024 * 1024),
            WatermarkService("ОБРАЗЕЦ", 1024, "JPEG", 82),
            self.provider,
            processing_router=empty_router,
            processing_executor=HybridProcessingExecutor(
                self.provider, empty_router.catalog, FixedMaskSegmenter(mask), settings.temp_dir
            ),
        )
        session = service.start_session("max", "owner-empty", source_path)
        with self.assertRaises(AssetUnavailableError):
            service.generate(
                session.session_id,
                "Замени фон на реалистичные скалистые горы",
                "asset-missing",
            )
        with service.database.read() as connection:
            attempts = connection.execute("SELECT COUNT(*) FROM generation_attempts").fetchone()[0]
        self.assertEqual(attempts, 0)
        self.assertEqual(self.provider.calls, 0)

    def test_mode_specific_prompts_are_english_and_limit_scope(self) -> None:
        parent = parse_edit_intent("Замени фон на реалистичные скалистые горы")
        parent_processing = self.router.route(
            parent, source_orientation="portrait", source_aspect_ratio=2 / 3
        )
        correction = merge_edit_plans(
            parent, parse_edit_intent("Поменяй куртку", mode="correction")
        )
        local = self.router.route(correction, parent=parent_processing)
        prompt = build_provider_prompt(correction, local)
        self.assertTrue(prompt.isascii())
        self.assertIn(
            "Edit every explicitly targeted region sufficiently",
            prompt,
        )
        main_section = prompt.split("PRESERVE", 1)[0]
        self.assertNotIn("Replace the background with realistic rocky mountains", main_section)
        enhancement = self.router.route(parse_edit_intent("Улучши качество"))
        enhancement_prompt = build_provider_prompt(
            parse_edit_intent("Улучши качество"), enhancement
        )
        self.assertIn("Do not add objects", enhancement_prompt)
        self.assertNotIn("Улучши", enhancement_prompt)

    def test_processing_plan_json_and_old_scene_json_are_compatible(self) -> None:
        plan = self.router.route(parse_edit_intent("Улучши качество"))
        self.assertEqual(ProcessingPlan.from_json(plan.to_json()), plan)
        payload = parse_edit_intent("Фон на скалы").to_dict()
        payload["scene"]["background"].pop("category")
        payload["scene"]["background"].pop("realism")
        payload["scene"]["background"].pop("blur")
        payload["scene"]["background"].pop("source")
        payload["schema_version"] = 2
        restored = EditPlan.from_dict(payload)
        self.assertIsNone(restored.scene.background.category)
        self.assertEqual(restored.scene.background.source, "auto")

    def test_no_random_internet_download_code_and_orphan_cleanup_is_scoped(self) -> None:
        for name in ("background_assets.py", "processing_router.py", "processing_pipeline.py"):
            source = (Path(__file__).parents[1] / "app" / name).read_text(encoding="utf-8")
            self.assertNotIn("requests.get", source)
            self.assertNotIn("httpx.get", source)
            self.assertNotIn("urlopen", source)
        temp = self.root / "orphan-temp"
        temp.mkdir()
        stale = temp / "pixora-mask-stale"
        stale.mkdir()
        keep = temp / "unrelated"
        keep.mkdir()
        old = time.time() - 7200
        import os
        os.utime(stale, (old, old))
        removed = cleanup_processing_temp(temp, older_than_seconds=3600)
        self.assertEqual(removed, (stale,))
        self.assertFalse(stale.exists())
        self.assertTrue(keep.exists())
