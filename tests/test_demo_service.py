import hashlib
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from PIL import Image, ImageChops

from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
from app.direct_prompt import build_direct_prompt
from app.domain import (
    ConcurrentGenerationError,
    CooldownError,
    DailyBudgetError,
    DeliveryError,
    DemoExpiredError,
    DemoLimitError,
    InvalidInputError,
    PaymentRequiredError,
    PolicyRejectedError,
    ProviderInvalidRequestError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.image_provider import FakeImageProvider
from app.storage import PrivateStorage
from app.watermark import WatermarkService, _font


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class BlockingProvider(FakeImageProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def edit(self, source_path: Path, prompt: str):
        self.entered.set()
        self.release.wait(timeout=5)
        return super().edit(source_path, prompt)


class RecordingProvider(FakeImageProvider):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def edit(self, source_path: Path, prompt: str):
        self.prompts.append(prompt)
        return super().edit(source_path, prompt)


class RecordingGeminiProvider(RecordingProvider):
    name = "gemini"
    model = "gemini-3-pro-image"


class MultiSourceRecordingProvider(FakeImageProvider):
    def __init__(self, fail=None) -> None:
        super().__init__(fail=fail)
        self.sources: tuple[Path, ...] = ()
        self.prompt = ""

    def edit_many(self, source_paths: tuple[Path, ...], prompt: str):
        self.sources = source_paths
        self.prompt = prompt
        return super().edit_many(source_paths, prompt)


class MultiSourceRecordingGeminiProvider(MultiSourceRecordingProvider):
    name = "gemini"
    model = "gemini-3-pro-image"


class DemoServiceTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        for name in ("data", "logs", "temp"):
            (self.base / name).mkdir()
        self.settings = Settings(
            "", "fake-image-edit-v1", "test", self.base,
            demo_min_request_interval_seconds=1,
            demo_daily_cost_limit_rub=100.0,
            demo_daily_generation_limit=20,
            demo_estimated_cost_rub_per_generation=10.0,
            max_source_file_size_mb=1,
        )
        self.clock = Clock()
        self.source = self.base / "source.png"
        Image.new("RGB", (1400, 900), "#c0d8ff").save(self.source)
        self.original_source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def service(self, provider=None, delivery=None, settings=None) -> DemoService:
        settings = settings or self.settings
        return DemoService(
            settings,
            Database(settings.database_path),
            PrivateStorage(settings.users_dir, settings.max_source_file_size_mb * 1024 * 1024),
            WatermarkService(
                settings.demo_watermark_text,
                settings.demo_max_dimension,
                settings.demo_output_format,
                settings.demo_jpeg_quality,
            ),
            provider or FakeImageProvider(),
            deliver_preview=delivery,
            clock=self.clock,
        )

    def test_one_account_can_use_different_photos_without_new_free_credits(self) -> None:
        service = self.service()
        first = service.start_session("max", "user-1", self.source)
        same = service.start_session("max", "user-1", self.source)
        self.assertEqual(first.session_id, same.session_id)
        other = self.base / "other.png"
        Image.new("RGB", (200, 200), "red").save(other)
        changed = service.start_session("max", "user-1", other)
        self.assertEqual(changed.session_id, first.session_id)
        self.assertNotEqual(changed.source_path.read_bytes(), self.source.read_bytes())
        self.assertEqual(service.commerce.balance(changed.user_id).available, 2)
        with service.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM generation_credit_lots WHERE user_id=?",
                    (changed.user_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM gallery_items WHERE user_id=?",
                    (changed.user_id,),
                ).fetchone()[0],
                2,
            )

    def test_two_sources_use_one_provider_call_one_credit_and_preserve_lineage(self) -> None:
        provider = MultiSourceRecordingGeminiProvider()
        service = self.service(provider=provider)
        session = service.start_session("max", "two-source-user", self.source)
        second = self.base / "second.png"
        Image.new("RGB", (500, 700), "red").save(second)
        stored_second = service.add_secondary_source(session.session_id, second)
        user_prompt = "Муж меня обнимает"

        result = service.generate(
            session.session_id,
            user_prompt,
            "two-source-attempt",
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.sources, (session.source_path, stored_second))
        self.assertEqual(provider.prompt, user_prompt)
        self.assertEqual(provider.prompt.encode("utf-8"), user_prompt.encode("utf-8"))
        self.assertNotIn("Сохрани", provider.prompt)
        self.assertNotIn("Измени только", provider.prompt)
        self.assertEqual(result.remaining_generations, 1)
        with service.database.read() as connection:
            attempt = connection.execute(
                "SELECT * FROM generation_attempts WHERE id=?", (result.attempt_id,)
            ).fetchone()
            version = connection.execute(
                "SELECT * FROM gallery_versions WHERE attempt_id=?", (result.attempt_id,)
            ).fetchone()
            self.assertEqual(attempt["secondary_source_path"], str(stored_second))
            self.assertEqual(version["secondary_source_path"], str(stored_second))

    def test_two_source_failure_and_third_source_do_not_consume_credit(self) -> None:
        provider = MultiSourceRecordingProvider(
            fail=ProviderUnavailableError("offline")
        )
        service = self.service(provider=provider)
        session = service.start_session("max", "two-source-failure", self.source)
        second = self.base / "second-failure.png"
        Image.new("RGB", (400, 400), "red").save(second)
        service.add_secondary_source(session.session_id, second)
        with self.assertRaisesRegex(InvalidInputError, "максимум 2"):
            service.add_secondary_source(session.session_id, second)

        with self.assertRaises(ProviderUnavailableError):
            service.generate(session.session_id, "Сделай коллаж", "two-fail")

        self.assertEqual(provider.calls, 1)
        self.assertEqual(service.commerce.balance(session.user_id).available, 2)

    def test_same_source_reupload_reactivates_expired_session_without_new_quota(self) -> None:
        settings = replace(self.settings, demo_session_ttl_minutes=1)
        service = self.service(settings=settings)
        first = service.start_session("max", "resume-user", self.source)
        self.clock.advance(61)
        with self.assertRaises(DemoExpiredError):
            service.generate(first.session_id, "Светлый фон", "expired-attempt")

        resumed = service.start_session("max", "resume-user", self.source)
        self.assertEqual(resumed.session_id, first.session_id)
        self.assertEqual(resumed.successful_generations, 0)
        self.assertEqual(resumed.max_generations, first.max_generations)
        with service.database.read() as connection:
            row = connection.execute(
                "SELECT status,expires_at,source_file_path FROM demo_sessions WHERE id=?",
                (first.session_id,),
            ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertGreater(datetime.fromisoformat(row["expires_at"]), self.clock())
        self.assertTrue(Path(row["source_file_path"]).is_file())

    def test_resume_uses_stored_source_without_upload_or_new_quota(self) -> None:
        settings = replace(self.settings, demo_session_ttl_minutes=1)
        service = self.service(settings=settings)
        first = service.start_session("max", "stored-user", self.source)
        self.clock.advance(61)

        resumed = service.resume_session("max", "stored-user")
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.session_id, first.session_id)
        self.assertEqual(resumed.source_path, first.source_path)
        self.assertEqual(resumed.successful_generations, 0)
        self.assertEqual(resumed.max_generations, first.max_generations)
        with service.database.read() as connection:
            row = connection.execute(
                "SELECT status,expires_at FROM demo_sessions WHERE id=?",
                (first.session_id,),
            ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertGreater(datetime.fromisoformat(row["expires_at"]), self.clock())
        self.assertIsNone(service.resume_session("max", "unknown-user"))

    def test_two_successes_only_and_successful_delivery_debits_once(self) -> None:
        service = self.service()
        session = service.start_session("max", "user-2", self.source)
        for index in range(2):
            result = service.generate(session.session_id, "Светлый фон", f"event-{index}")
            self.assertEqual(result.remaining_generations, 1 - index)
            self.clock.advance(2)
        replay = service.generate(session.session_id, "Светлый фон", "event-1")
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(replay.remaining_generations, 0)
        with self.assertRaises(DemoLimitError):
            service.generate(session.session_id, "Ещё", "event-3")

    def test_technical_and_policy_failures_do_not_debit(self) -> None:
        for user, failure, expected_status in (
            ("technical", RuntimeError("network"), "failed_technical"),
            ("unavailable", ProviderUnavailableError("network"), "failed_technical"),
            ("timeout", ProviderTimeoutError("timeout"), "failed_technical"),
            ("invalid", ProviderInvalidRequestError("invalid"), "failed_technical"),
            ("policy", PolicyRejectedError("blocked"), "rejected_policy"),
        ):
            service = self.service(provider=FakeImageProvider(fail=failure))
            session = service.start_session("max", user, self.source)
            with self.assertRaises(type(failure)):
                service.generate(session.session_id, "test", f"event-{user}")
            with service.database.read() as connection:
                stored = connection.execute(
                    "SELECT successful_generations FROM demo_sessions WHERE id=?",
                    (session.session_id,),
                ).fetchone()[0]
                status = connection.execute(
                    "SELECT status FROM generation_attempts WHERE session_id=?",
                    (session.session_id,),
                ).fetchone()[0]
                reservation = connection.execute(
                    "SELECT status FROM generation_credit_reservations WHERE attempt_id=(SELECT id FROM generation_attempts WHERE session_id=?)",
                    (session.session_id,),
                ).fetchone()[0]
            self.assertEqual(stored, 0)
            self.assertEqual(status, expected_status)
            self.assertEqual(reservation, "released")

    def test_delivery_failure_does_not_debit(self) -> None:
        service = self.service(delivery=lambda _path, _attempt: False)
        session = service.start_session("max", "delivery", self.source)
        with self.assertRaises(DeliveryError):
            service.generate(session.session_id, "test", "delivery-event")
        with service.database.read() as connection:
            row = connection.execute(
                "SELECT successful_generations FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()
        self.assertEqual(row[0], 0)

    def test_concurrent_request_is_blocked(self) -> None:
        provider = BlockingProvider()
        service = self.service(provider=provider)
        session = service.start_session("max", "concurrent", self.source)
        errors = []

        def first_request() -> None:
            try:
                service.generate(session.session_id, "first", "concurrent-1")
            except Exception as exc:  # pragma: no cover - assertion captures unexpected failure
                errors.append(exc)

        thread = threading.Thread(target=first_request)
        thread.start()
        self.assertTrue(provider.entered.wait(timeout=2))
        with self.assertRaises(ConcurrentGenerationError):
            service.generate(session.session_id, "second", "concurrent-2")
        provider.release.set()
        thread.join(timeout=5)
        self.assertEqual(errors, [])

    def test_cooldown_and_daily_limits(self) -> None:
        service = self.service()
        first = service.start_session("max", "cooldown", self.source)
        service.generate(first.session_id, "one", "cooldown-1")
        with self.assertRaises(CooldownError):
            service.generate(first.session_id, "two", "cooldown-2")

        limited = replace(
            self.settings,
            base_dir=self.base / "limited",
            demo_daily_generation_limit=1,
            demo_daily_cost_limit_rub=10.0,
        )
        for name in ("data", "logs", "temp"):
            (limited.base_dir / name).mkdir(parents=True, exist_ok=True)
        limited_source = limited.base_dir / "source.png"
        Image.new("RGB", (200, 200), "blue").save(limited_source)
        limited_service = self.service(settings=limited)
        one = limited_service.start_session("max", "daily-one", limited_source)
        limited_service.generate(one.session_id, "one", "daily-1")
        self.clock.advance(2)
        two = limited_service.start_session("max", "daily-two", limited_source)
        with self.assertRaises(DailyBudgetError):
            limited_service.generate(two.session_id, "two", "daily-2")

    def test_budget_guard_stops_provider_without_stopping_the_service(self) -> None:
        for guarded_settings in (
            replace(
                self.settings,
                image_provider="openai",
                openai_image_requests_enabled=False,
            ),
            replace(
                self.settings,
                image_provider="openai",
                openai_balance_usd=0.5,
                openai_balance_critical_usd=1.0,
            ),
        ):
            provider = FakeImageProvider()
            service = self.service(provider=provider, settings=guarded_settings)
            session = service.start_session("max", "guarded", self.source)
            with self.assertRaises(ProviderUnavailableError):
                service.generate(session.session_id, "change background", "guarded")
            self.assertEqual(provider.calls, 0)
            with service.database.read() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM generation_attempts"
                    ).fetchone()[0],
                    0,
                )

    def test_gemini_direct_prompt_is_exact_for_initial_and_correction(self) -> None:
        provider = RecordingGeminiProvider()
        settings = replace(self.settings, image_direct_prompt_enabled=True)
        service = self.service(provider=provider, settings=settings)
        session = service.start_session("max", "direct-prompt", self.source)

        first_text = "Изменить размер для загрузки на сотовый телефон"
        second_text = "Муж меня обнимает"
        with patch(
            "app.demo_service.build_provider_prompt",
            side_effect=AssertionError("technical prompt builder must be bypassed"),
        ), patch(
            "app.demo_service.parse_edit_intent",
            side_effect=AssertionError("legacy intent parser must be bypassed"),
        ):
            first = service.generate(session.session_id, first_text, "direct-1")
            self.clock.advance(2)
            with service.database.read() as connection:
                first_version = connection.execute(
                    "SELECT * FROM gallery_versions WHERE attempt_id=?",
                    (first.attempt_id,),
                ).fetchone()
            second = service.generate(
                session.session_id,
                second_text,
                "direct-2",
                correction=True,
                parent_version_id=first_version["id"],
            )

        self.assertEqual(len(provider.prompts), 2)
        self.assertEqual(provider.prompts, [first_text, second_text])
        self.assertEqual(
            [value.encode("utf-8") for value in provider.prompts],
            [first_text.encode("utf-8"), second_text.encode("utf-8")],
        )
        for provider_prompt in provider.prompts:
            self.assertNotIn("Сохрани", provider_prompt)
            self.assertNotIn("Измени только", provider_prompt)
            self.assertNotIn("preservation", provider_prompt.casefold())
        self.assertEqual(second.remaining_generations, 0)
        with service.database.read() as connection:
            attempts = connection.execute(
                """SELECT prompt,provider_prompt,prompt_builder_version
                   FROM generation_attempts ORDER BY created_at,id"""
            ).fetchall()
            versions = connection.execute(
                "SELECT * FROM gallery_versions ORDER BY version_number"
            ).fetchall()
            payment_rows = sum(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("payment_intents", "payment_orders", "unlock_entitlements")
            )

        self.assertEqual([row["provider_prompt"] for row in attempts], provider.prompts)
        self.assertEqual(
            [row["prompt"] for row in attempts],
            [first_text, second_text],
        )
        self.assertEqual(
            [row["provider_prompt"] for row in attempts],
            [row["prompt"] for row in attempts],
        )
        self.assertTrue(
            all(row["prompt_builder_version"] == "direct-unicode-v5" for row in attempts)
        )
        self.assertEqual(len(versions), 2)
        self.assertIsNone(versions[0]["parent_version_id"])
        self.assertEqual(versions[1]["parent_version_id"], versions[0]["id"])
        self.assertEqual(versions[1]["source_version_id"], versions[0]["id"])
        self.assertEqual(versions[1]["source_path"], versions[0]["original_path"])
        self.assertEqual(payment_rows, 0)

    def test_direct_prompt_mode_bypasses_intent_processing_router(self) -> None:
        provider = RecordingProvider()
        router = Mock()
        settings = replace(self.settings, image_direct_prompt_enabled=True)
        service = DemoService(
            settings,
            Database(settings.database_path),
            PrivateStorage(
                settings.users_dir, settings.max_source_file_size_mb * 1024 * 1024
            ),
            WatermarkService(
                settings.demo_watermark_text,
                settings.demo_max_dimension,
                settings.demo_output_format,
                settings.demo_jpeg_quality,
            ),
            provider,
            processing_router=router,
            clock=self.clock,
        )
        session = service.start_session("max", "direct-router", self.source)

        service.generate(session.session_id, "сделай меня красивее", "direct-router-1")

        router.route.assert_not_called()
        self.assertEqual(provider.prompts, [build_direct_prompt("сделай меня красивее")])

    def test_default_prompt_mode_keeps_current_technical_builder(self) -> None:
        provider = RecordingProvider()
        settings = replace(self.settings, image_direct_prompt_enabled=False)
        service = self.service(provider=provider, settings=settings)
        session = service.start_session("max", "layered-prompt", self.source)
        user_text = "замени фон"

        result = service.generate(session.session_id, user_text, "layered-1")

        self.assertEqual(len(provider.prompts), 1)
        self.assertNotEqual(provider.prompts[0], user_text)
        self.assertIn("PRIORITY ORDER", provider.prompts[0])
        self.assertIn("PRESERVE", provider.prompts[0])
        with service.database.read() as connection:
            attempt = connection.execute(
                "SELECT provider_prompt,prompt_builder_version FROM generation_attempts WHERE id=?",
                (result.attempt_id,),
            ).fetchone()
        self.assertEqual(attempt["provider_prompt"], provider.prompts[0])
        self.assertEqual(
            attempt["prompt_builder_version"], "technical-en-v5-target-completion"
        )

    def test_direct_prompt_mode_bypasses_builder_for_scenario_and_repeat(self) -> None:
        provider = RecordingProvider()
        settings = replace(self.settings, image_direct_prompt_enabled=True)
        service = self.service(provider=provider, settings=settings)
        session = service.start_session("max", "direct-scenario", self.source)

        first = service.generate(
            session.session_id,
            "Применить выбранный сценарий",
            "scenario-1",
            scenario_id="resume",
        )
        self.clock.advance(2)
        with service.database.read() as connection:
            first_version_id = connection.execute(
                "SELECT id FROM gallery_versions WHERE attempt_id=?", (first.attempt_id,)
            ).fetchone()[0]
        service.generate(
            session.session_id,
            "Другой вариант",
            "scenario-2",
            repeat=True,
            parent_version_id=first_version_id,
        )

        self.assertEqual(len(provider.prompts), 2)
        self.assertEqual(
            provider.prompts,
            [
                build_direct_prompt("Применить выбранный сценарий"),
                build_direct_prompt("Другой вариант"),
            ],
        )
        self.assertTrue(all("PRIORITY ORDER" not in prompt for prompt in provider.prompts))
        with service.database.read() as connection:
            versions = connection.execute(
                """SELECT prompt_builder_version,parent_version_id
                   FROM gallery_versions ORDER BY version_number"""
            ).fetchall()
        self.assertTrue(
            all(
                row["prompt_builder_version"] == "direct-unicode-v5"
                for row in versions
            )
        )
        self.assertEqual(versions[1]["parent_version_id"], first_version_id)

    def test_watermark_preview_is_reduced_and_original_unchanged(self) -> None:
        service = self.service()
        session = service.start_session("max", "watermark", self.source)
        result = service.generate(session.session_id, "test", "watermark-1")
        with service.database.read() as connection:
            original_path = Path(connection.execute(
                "SELECT original_result_path FROM generation_attempts WHERE id=?", (result.attempt_id,)
            ).fetchone()[0])
        original_before = hashlib.sha256(original_path.read_bytes()).hexdigest()
        with Image.open(original_path) as original, Image.open(result.preview_path) as preview:
            self.assertLessEqual(max(preview.size), 1024)
            self.assertNotEqual(ImageChops.difference(original.convert("RGB").resize(preview.size), preview.convert("RGB")).getbbox(), None)
        self.assertEqual(hashlib.sha256(original_path.read_bytes()).hexdigest(), original_before)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_source_hash)
        self.assertIsNotNone(_font(40, "ОБРАЗЕЦ").getmask("ОБРАЗЕЦ").getbbox())
        self.assertFalse(hasattr(result, "original_path"))

    def test_invalid_file_size_prompt_expiry_delete_and_unlock_guard(self) -> None:
        service = self.service()
        invalid = self.base / "not-image.jpg"
        invalid.write_text("not an image", encoding="utf-8")
        with self.assertRaises(InvalidInputError):
            service.start_session("max", "invalid", invalid)
        session = service.start_session("max", "misc", self.source)
        with self.assertRaises(InvalidInputError):
            service.generate(session.session_id, "x" * 1501, "long")
        result = service.generate(session.session_id, "valid", "valid")
        intent = service.unlock_original(result.attempt_id, "pay-1")
        with self.assertRaises(PaymentRequiredError):
            service.original_for_paid_intent(intent)
        service.confirm_payment(intent)
        service.confirm_payment(intent)
        self.assertTrue(service.original_for_paid_intent(intent).is_file())
        with service.database.read() as connection:
            gallery_item_id = connection.execute(
                "SELECT gallery_item_id FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
            retention_until = connection.execute(
                "SELECT retention_until FROM gallery_items WHERE id=?", (gallery_item_id,)
            ).fetchone()[0]
        versions = service.gallery.list_versions(session.user_id, gallery_item_id)
        self.assertTrue(versions[0].original_path.is_file())
        self.assertGreaterEqual(
            datetime.fromisoformat(retention_until),
            self.clock() + timedelta(days=self.settings.paid_retention_days),
        )
        root = service.storage.session_root(session.user_id, session.session_id)
        self.assertTrue(root.exists())
        service.delete_session(session.session_id)
        self.assertTrue(root.exists())
        with service.database.read() as connection:
            deleted = connection.execute(
                "SELECT deleted FROM gallery_items WHERE id=(SELECT gallery_item_id FROM demo_sessions WHERE id=?)",
                (session.session_id,),
            ).fetchone()[0]
        self.assertEqual(deleted, 1)

        expired_settings = replace(self.settings, base_dir=self.base / "expired", demo_session_ttl_minutes=1)
        for name in ("data", "logs", "temp"):
            (expired_settings.base_dir / name).mkdir(parents=True, exist_ok=True)
        expired_source = expired_settings.base_dir / "source.png"
        Image.new("RGB", (200, 200), "green").save(expired_source)
        expired_service = self.service(settings=expired_settings)
        expired = expired_service.start_session("max", "expired", expired_source)
        self.clock.advance(61)
        with self.assertRaises(DemoExpiredError):
            expired_service.generate(expired.session_id, "test", "expired-event")

    def test_oversized_source_is_rejected(self) -> None:
        tiny_limit = replace(self.settings, max_source_file_size_mb=1)
        oversized = self.base / "oversized.png"
        oversized.write_bytes(b"0" * (1024 * 1024 + 1))
        with self.assertRaises(InvalidInputError):
            self.service(settings=tiny_limit).start_session("max", "oversized", oversized)

    def test_restart_recovers_processing_attempt_without_debit(self) -> None:
        service = self.service()
        session = service.start_session("max", "restart", self.source)
        with service.database.transaction() as connection:
            connection.execute(
                """INSERT INTO generation_attempts(
                       id,idempotency_key,session_id,user_id,prompt,status,started_at,
                       provider,model,source_path,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "interrupted",
                    "restart-event",
                    session.session_id,
                    session.user_id,
                    "test",
                    "processing",
                    self.clock().isoformat(),
                    "fake",
                    "fake-image-edit-v1",
                    str(session.source_path),
                    self.clock().isoformat(),
                ),
            )
        Database(self.settings.database_path).recover_interrupted_runtime()
        with service.database.read() as connection:
            attempt = connection.execute(
                "SELECT status,technical_refund FROM generation_attempts WHERE id='interrupted'"
            ).fetchone()
            count = connection.execute(
                "SELECT successful_generations FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
        self.assertEqual(tuple(attempt), ("failed_technical", 1))
        self.assertEqual(count, 0)
