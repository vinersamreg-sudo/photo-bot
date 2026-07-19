from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image

from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
from app.domain import DeliveryError, ProviderResult, ProviderUnavailableError
from app.image_provider import ContextAwareImageProvider, FakeImageProvider
from app.provider_context import ProviderContextService
from app.storage import PrivateStorage
from app.watermark import WatermarkService


UTC = timezone.utc


class MemoryProvider(FakeImageProvider):
    def __init__(self, *, fail_context: bool = False) -> None:
        super().__init__()
        self.fail_context = fail_context
        self.context_calls = []
        self.sources: list[Path] = []

    def edit(self, source_path: Path, prompt: str) -> ProviderResult:
        self.sources.append(source_path)
        return super().edit(source_path, prompt)

    def edit_with_context(self, source_path, prompt, context) -> ProviderResult:
        self.context_calls.append(context)
        self.sources.append(source_path)
        if self.fail_context:
            raise ProviderUnavailableError("context unavailable")
        result = super().edit(source_path, prompt)
        return replace(
            result,
            provider_name="openai-responses",
            provider_model="gpt-5.4-mini",
            image_model="gpt-image-2",
            provider_response_id=f"resp-{len(self.context_calls)}",
            provider_mode="responses",
            context_depth=context.depth,
            http_status=200,
            provider_duration_ms=25,
        )


class ProviderContextIntegrationTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock_value = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
        self.settings = Settings(
            "test",
            "gpt-image-2",
            "test",
            self.root,
            demo_min_request_interval_seconds=1,
            openai_conversation_memory_enabled=True,
            openai_responses_image_enabled=True,
            openai_conversation_retention_enabled=True,
        )
        self.database = Database(self.settings.database_path)
        self.storage = PrivateStorage(self.settings.users_dir, 5 * 1024 * 1024)
        self.provider = MemoryProvider()
        self.contexts = ProviderContextService(
            self.settings, self.database, clock=lambda: self.clock_value
        )
        self.service = DemoService(
            self.settings,
            self.database,
            self.storage,
            WatermarkService("SAMPLE", 512, "JPEG", 80),
            self.provider,
            clock=lambda: self.clock_value,
            provider_context_service=self.contexts,
        )
        source = self.root / "source.png"
        Image.new("RGB", (80, 100), "white").save(source)
        self.session = self.service.start_session("test", "owner", source)
        with self.database.read() as connection:
            self.item_id = connection.execute(
                "SELECT gallery_item_id FROM demo_sessions WHERE id=?",
                (self.session.session_id,),
            ).fetchone()[0]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _advance(self) -> None:
        self.clock_value += timedelta(seconds=2)

    def _generate(self, key: str, **kwargs):
        self._advance()
        return self.service.generate(
            self.session.session_id,
            kwargs.pop("prompt", "replace background with rocky mountains"),
            key,
            **kwargs,
        )

    def _versions(self):
        return self.service.gallery.list_versions(self.session.user_id, self.item_id)

    def test_01_enabled_initial_request_creates_response_backed_version(self) -> None:
        self._generate("initial")
        version = self._versions()[0]
        self.assertEqual((version.provider_mode, version.provider_response_id), ("responses", "resp-1"))
        self.assertIsNotNone(version.provider_context_id)

    def test_02_correction_uses_parent_response_and_parent_original(self) -> None:
        self._generate("initial")
        first = self._versions()[0]
        self._generate("correction", prompt="make only the jacket green", correction=True)
        second = self._versions()[1]
        self.assertEqual(self.provider.context_calls[1].previous_response_id, "resp-1")
        with self.database.read() as connection:
            parent_original = connection.execute(
                "SELECT original_path FROM gallery_versions WHERE id=?", (first.id,)
            ).fetchone()[0]
        self.assertEqual(str(self.provider.sources[1]), parent_original)
        self.assertEqual(second.parent_version_id, first.id)

    def test_03_branch_correction_uses_selected_older_response(self) -> None:
        self._generate("initial")
        first = self._versions()[0]
        self._generate("second", prompt="make only the jacket green", correction=True)
        self._generate(
            "branch",
            prompt="make only the jacket blue",
            correction=True,
            parent_version_id=first.id,
        )
        self.assertEqual(self.provider.context_calls[2].previous_response_id, "resp-1")
        self.assertEqual(self._versions()[2].parent_version_id, first.id)

    def test_04_repeat_is_stateless_and_does_not_advance_context(self) -> None:
        self._generate("initial")
        first = self._versions()[0]
        before = self.contexts.get_context(self.item_id)["last_response_id"]
        self._generate("repeat", repeat=True, parent_version_id=first.id)
        second = self._versions()[1]
        after = self.contexts.get_context(self.item_id)["last_response_id"]
        self.assertEqual((second.provider_mode, before, after), ("stateless", "resp-1", "resp-1"))

    def test_05_all_flags_off_preserve_old_stateless_flow(self) -> None:
        settings = replace(
            self.settings,
            openai_conversation_memory_enabled=False,
            openai_responses_image_enabled=False,
            openai_conversation_retention_enabled=False,
        )
        service = DemoService(
            settings,
            self.database,
            self.storage,
            WatermarkService("SAMPLE", 512, "JPEG", 80),
            self.provider,
            clock=lambda: self.clock_value,
            provider_context_service=ProviderContextService(settings, self.database),
        )
        self._advance()
        service.generate(self.session.session_id, "replace background", "off")
        self.assertEqual(service.gallery.list_versions(self.session.user_id, self.item_id)[0].provider_mode, "stateless")

    def test_06_delivery_failure_creates_no_version_and_does_not_debit(self) -> None:
        self._advance()
        with self.assertRaises(DeliveryError):
            self.service.generate(
                self.session.session_id,
                "replace background",
                "delivery-fail",
                delivery_override=lambda *_: False,
            )
        self.assertEqual(self._versions(), [])
        with self.database.read() as connection:
            session = connection.execute(
                "SELECT successful_generations FROM demo_sessions WHERE id=?",
                (self.session.session_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT provider_response_id,status FROM generation_attempts WHERE idempotency_key='delivery-fail'"
            ).fetchone()
        self.assertEqual((session[0], attempt[1]), (0, "delivery_failed"))
        self.assertEqual(attempt[0], "resp-1")

    def test_07_invalid_context_falls_back_once_and_delivers(self) -> None:
        contextual = MemoryProvider(fail_context=True)
        stateless = FakeImageProvider()
        wrapper = ContextAwareImageProvider(stateless, contextual)
        service = DemoService(
            self.settings,
            self.database,
            self.storage,
            WatermarkService("SAMPLE", 512, "JPEG", 80),
            wrapper,
            clock=lambda: self.clock_value,
            provider_context_service=self.contexts,
        )
        self._advance()
        service.generate(self.session.session_id, "replace background", "fallback")
        version = service.gallery.list_versions(self.session.user_id, self.item_id)[0]
        self.assertEqual((stateless.calls, version.provider_mode), (1, "stateless"))
        self.assertEqual(version.context_fallback_reason, "ProviderUnavailableError")

    def test_08_fallback_version_does_not_claim_context_use(self) -> None:
        contextual = MemoryProvider(fail_context=True)
        wrapper = ContextAwareImageProvider(FakeImageProvider(), contextual)
        service = DemoService(
            self.settings,
            self.database,
            self.storage,
            WatermarkService("SAMPLE", 512, "JPEG", 80),
            wrapper,
            clock=lambda: self.clock_value,
            provider_context_service=self.contexts,
        )
        self._advance()
        result = service.generate(self.session.session_id, "replace background", "fallback-2")
        with self.database.read() as connection:
            version = connection.execute(
                "SELECT provider_context_used,provider_context_fallback_reason FROM gallery_versions WHERE attempt_id=?",
                (result.attempt_id,),
            ).fetchone()
        self.assertEqual((version[0], version[1]), (0, "ProviderUnavailableError"))

    def test_09_version_contains_hashes_not_duplicate_provider_payload(self) -> None:
        result = self._generate("hash")
        with self.database.read() as connection:
            version = connection.execute(
                "SELECT effective_prompt_hash,scene_intent_hash,provider_usage_json FROM gallery_versions WHERE attempt_id=?",
                (result.attempt_id,),
            ).fetchone()
        self.assertEqual((len(version[0]), len(version[1])), (64, 64))
        self.assertIn("fake", version[2])

    def test_10_provider_failure_creates_no_gallery_version(self) -> None:
        provider = MemoryProvider(fail_context=True)
        service = DemoService(
            self.settings,
            self.database,
            self.storage,
            WatermarkService("SAMPLE", 512, "JPEG", 80),
            provider,
            clock=lambda: self.clock_value,
            provider_context_service=self.contexts,
        )
        self._advance()
        with self.assertRaises(ProviderUnavailableError):
            service.generate(self.session.session_id, "replace background", "provider-fail")
        self.assertEqual(service.gallery.list_versions(self.session.user_id, self.item_id), [])

    def test_11_missing_parent_response_reason_is_persisted_on_stateless_version(self) -> None:
        self._generate("initial")
        first = self._versions()[0]
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE gallery_versions SET provider_response_id=NULL WHERE id=?",
                (first.id,),
            )
        self._generate(
            "missing-parent-response",
            prompt="make only the jacket green",
            correction=True,
            parent_version_id=first.id,
        )
        version = self._versions()[1]
        self.assertEqual(version.provider_mode, "stateless")
        self.assertEqual(version.context_fallback_reason, "missing_parent_response_id")
