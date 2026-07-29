import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
from app.domain import DeliveryError, InvalidInputError
from app.edit_intent import EditPlan
from app.image_provider import FakeImageProvider
from app.storage import PrivateStorage
from app.watermark import WatermarkService


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self) -> None:
        self.value += timedelta(seconds=2)


class CapturingProvider(FakeImageProvider):
    def __init__(self, fail=None) -> None:
        super().__init__(fail=fail)
        self.sources: list[Path] = []
        self.prompts: list[str] = []

    def edit(self, source_path: Path, prompt: str):
        self.sources.append(source_path)
        self.prompts.append(prompt)
        return super().edit(source_path, prompt)


class AiBrainIntegrationTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        for name in ("data", "logs", "temp"):
            (self.base / name).mkdir()
        self.settings = Settings(
            "", "fake-image-edit-v1", "test", self.base,
            demo_min_request_interval_seconds=1,
        )
        self.clock = Clock()
        self.database = Database(self.settings.database_path)
        self.source = self.base / "synthetic.png"
        Image.new("RGB", (640, 480), "#dce8f2").save(self.source)

    def service(self, provider=None, delivery=None) -> DemoService:
        return DemoService(
            self.settings,
            self.database,
            PrivateStorage(self.settings.users_dir, 15 * 1024 * 1024),
            WatermarkService("ОБРАЗЕЦ", 1024, "JPEG", 82),
            provider or CapturingProvider(),
            deliver_preview=delivery,
            clock=self.clock,
        )

    def _item_id(self, session_id: str) -> str:
        with self.database.read() as connection:
            return connection.execute(
                "SELECT gallery_item_id FROM demo_sessions WHERE id=?", (session_id,)
            ).fetchone()[0]

    def test_three_version_rocky_regression_uses_each_parent_original(self) -> None:
        provider = CapturingProvider()
        service = self.service(provider)
        session = service.start_session("max", "rock-user", self.source)
        service.generate(session.session_id, "Фон на скалы", "rocks-1")
        versions = service.gallery.list_versions(session.user_id, self._item_id(session.session_id))
        with self.database.read() as connection:
            original_v1 = Path(connection.execute(
                "SELECT original_path FROM gallery_versions WHERE id=?", (versions[0].id,)
            ).fetchone()[0])
        self.clock.advance()
        service.generate(
            session.session_id,
            "Переодень девушку в одежду для хайкинга, смени позу, фон не должен быть размыт",
            "rocks-2",
            correction=True,
            parent_version_id=versions[0].id,
        )
        versions = service.gallery.list_versions(session.user_id, self._item_id(session.session_id))
        with self.database.read() as connection:
            original_v2 = Path(connection.execute(
                "SELECT original_path FROM gallery_versions WHERE id=?", (versions[1].id,)
            ).fetchone()[0])
        self.clock.advance()
        with self.database.transaction() as connection:
            service.commerce.adjust_generation_credits(
                connection,
                user_id=session.user_id,
                delta=1,
                reason="three-version AI lineage regression",
                idempotency_key="rocks-regression-credit",
            )
        service.generate(
            session.session_id,
            "Сохрани эти же скалы. Убери только остаточное размытие фона. Остальное не меняй",
            "rocks-3",
            correction=True,
            parent_version_id=versions[1].id,
        )
        versions = service.gallery.list_versions(session.user_id, self._item_id(session.session_id))
        self.assertEqual(provider.sources, [session.source_path, original_v1, original_v2])
        self.assertEqual(versions[2].source_version_id, versions[1].id)
        self.assertEqual(versions[2].parent_version_id, versions[1].id)
        inherited = " ".join(versions[2].edit_plan.inherited_constraints).lower()
        self.assertIn("hiking clothing", inherited)
        self.assertIn("pose", inherited)
        self.assertIn("do not apply background blur", provider.prompts[2].lower())

    def test_repeat_reuses_parent_input_and_effective_intent(self) -> None:
        provider = CapturingProvider()
        service = self.service(provider)
        session = service.start_session("max", "repeat-user", self.source)
        service.generate(session.session_id, "Фон на скалы", "repeat-1")
        versions = service.gallery.list_versions(session.user_id, self._item_id(session.session_id))
        self.clock.advance()
        service.generate(
            session.session_id, "Другой вариант", "repeat-2",
            repeat=True, parent_version_id=versions[0].id,
        )
        versions = service.gallery.list_versions(session.user_id, self._item_id(session.session_id))
        self.assertEqual(provider.sources[1], versions[0].source_path)
        self.assertEqual(versions[1].edit_plan.mode, "repeat")
        self.assertEqual(
            versions[1].edit_plan.requested_changes,
            versions[0].edit_plan.requested_changes,
        )

    def test_missing_parent_version_fails_before_provider_and_attempt(self) -> None:
        provider = CapturingProvider()
        service = self.service(provider)
        session = service.start_session("max", "missing-parent", self.source)
        with self.assertRaises(InvalidInputError):
            service.generate(
                session.session_id, "Фон чётче", "missing-1",
                correction=True, parent_version_id="missing-version",
            )
        self.assertEqual(provider.calls, 0)
        with self.database.read() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM generation_attempts WHERE session_id=?",
                (session.session_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_provider_error_keeps_failed_plan_but_creates_no_version(self) -> None:
        provider = CapturingProvider(fail=RuntimeError("network"))
        service = self.service(provider)
        session = service.start_session("max", "provider-failure", self.source)
        with self.assertRaises(RuntimeError):
            service.generate(session.session_id, "Фон на скалы", "provider-fail-1")
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT status,edit_plan_json FROM generation_attempts WHERE session_id=?",
                (session.session_id,),
            ).fetchone()
            versions = connection.execute(
                "SELECT COUNT(*) FROM gallery_versions WHERE gallery_item_id=?",
                (self._item_id(session.session_id),),
            ).fetchone()[0]
        self.assertEqual(attempt["status"], "failed_technical")
        self.assertEqual(EditPlan.from_json(attempt["edit_plan_json"]).primary_action, "replace_background")
        self.assertEqual(versions, 0)

    def test_delivery_failure_does_not_create_gallery_version(self) -> None:
        service = self.service(CapturingProvider(), delivery=lambda _path, _attempt: False)
        session = service.start_session("max", "delivery-failure", self.source)
        with self.assertRaises(DeliveryError):
            service.generate(session.session_id, "Фон на скалы", "delivery-fail-1")
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT status,edit_plan_json FROM generation_attempts WHERE session_id=?",
                (session.session_id,),
            ).fetchone()
            version_count = connection.execute(
                "SELECT COUNT(*) FROM gallery_versions"
            ).fetchone()[0]
        self.assertEqual(attempt["status"], "delivery_failed")
        self.assertTrue(attempt["edit_plan_json"])
        self.assertEqual(version_count, 0)

    def test_feedback_is_upserted_without_user_text(self) -> None:
        service = self.service()
        session = service.start_session("max", "feedback-user", self.source)
        service.generate(session.session_id, "Фон на скалы", "feedback-1")
        version = service.gallery.list_versions(
            session.user_id, self._item_id(session.session_id)
        )[0]
        service.gallery.record_feedback(session.user_id, version.id, "negative")
        service.gallery.record_feedback(session.user_id, version.id, "positive")
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM version_feedback").fetchone()
            columns = {item[1] for item in connection.execute("PRAGMA table_info(version_feedback)")}
        self.assertEqual(row["sentiment"], "positive")
        self.assertNotIn("user_text", columns)

    def test_attempt_persists_separate_user_and_provider_prompts(self) -> None:
        service = self.service()
        session = service.start_session("max", "prompt-storage", self.source)
        result = service.generate(session.session_id, "Фон на скалы", "storage-1")
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM generation_attempts WHERE id=?", (result.attempt_id,)
            ).fetchone()
        self.assertEqual(row["prompt"], "Фон на скалы")
        self.assertNotEqual(row["provider_prompt"], row["prompt"])
        self.assertEqual(json.loads(row["edit_plan_json"])["source_user_text"], "Фон на скалы")
        self.assertEqual(
            row["prompt_builder_version"],
            "technical-en-v4-request-first",
        )
        self.assertTrue(row["provider_prompt"].isascii())
        self.assertEqual(json.loads(row["edit_plan_json"])["schema_version"], 3)
