import hashlib
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from PIL import Image, ImageChops

from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
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
    SourceReplacementError,
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

    def test_one_session_per_user_and_source_cannot_be_replaced(self) -> None:
        service = self.service()
        first = service.start_session("max", "user-1", self.source)
        same = service.start_session("max", "user-1", self.source)
        self.assertEqual(first.session_id, same.session_id)
        other = self.base / "other.png"
        Image.new("RGB", (200, 200), "red").save(other)
        with self.assertRaises(SourceReplacementError):
            service.start_session("max", "user-1", other)

    def test_five_successes_only_and_successful_delivery_debits_once(self) -> None:
        service = self.service()
        session = service.start_session("max", "user-2", self.source)
        for index in range(5):
            result = service.generate(session.session_id, "Светлый фон", f"event-{index}")
            self.assertEqual(result.remaining_generations, 4 - index)
            self.clock.advance(2)
        replay = service.generate(session.session_id, "Светлый фон", "event-4")
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(replay.remaining_generations, 0)
        with self.assertRaises(DemoLimitError):
            service.generate(session.session_id, "Ещё", "event-6")

    def test_technical_and_policy_failures_do_not_debit(self) -> None:
        for user, failure, expected_status in (
            ("technical", RuntimeError("network"), "failed_technical"),
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
            self.assertEqual(stored, 0)
            self.assertEqual(status, expected_status)

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
        root = service.storage.session_root(session.user_id, session.session_id)
        self.assertTrue(root.exists())
        service.delete_session(session.session_id)
        self.assertFalse(root.exists())

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
        Database(self.settings.database_path)
        with service.database.read() as connection:
            attempt = connection.execute(
                "SELECT status,technical_refund FROM generation_attempts WHERE id='interrupted'"
            ).fetchone()
            count = connection.execute(
                "SELECT successful_generations FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
        self.assertEqual(tuple(attempt), ("failed_technical", 1))
        self.assertEqual(count, 0)
