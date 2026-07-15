import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
from app.image_provider import FakeImageProvider
from app.max_application import MaxApplication, UNLOCK_PLACEHOLDER
from app.max_conversation import MaxConversationStore
from app.max_transport import MaxIncomingEvent, MaxTransportError
from app.storage import PrivateStorage
from app.watermark import WatermarkService


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeMaxTransport:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.messages = []
        self.images = []
        self.edits = []
        self.callbacks = []
        self.image_delivery = True
        self.fail_next_message = False

    def send_message(self, user_id, text, buttons=(), **kwargs):
        if self.fail_next_message:
            self.fail_next_message = False
            raise MaxTransportError("fake send failure")
        message_id = f"sent-{len(self.messages) + 1}"
        self.messages.append((user_id, text, tuple(buttons), kwargs, message_id))
        return message_id

    def edit_message(self, message_id, text, buttons=()):
        self.edits.append((message_id, text, tuple(buttons)))

    def answer_callback(self, callback_id, notification):
        self.callbacks.append((callback_id, notification))

    def download_image(self, _url, destination, _max_bytes):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, destination)
        return destination

    def send_image(self, user_id, image, caption, buttons):
        self.images.append((user_id, Path(image), caption, tuple(buttons)))
        return self.image_delivery


class MaxApplicationTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        for name in ("data", "logs", "temp"):
            (self.base / name).mkdir()
        self.source = self.base / "synthetic.png"
        Image.new("RGB", (320, 240), "#dde7f2").save(self.source)
        self.settings = Settings(
            "", "fake-image-edit-v1", "test", self.base,
            demo_min_request_interval_seconds=1,
        )
        self.clock = Clock()
        self.database = Database(self.settings.database_path)
        self.storage = PrivateStorage(
            self.settings.users_dir,
            self.settings.max_source_file_size_mb * 1024 * 1024,
        )
        self.provider = FakeImageProvider()
        self.demo = DemoService(
            self.settings,
            self.database,
            self.storage,
            WatermarkService("ОБРАЗЕЦ", 1024, "JPEG", 82),
            self.provider,
            clock=self.clock,
        )
        self.transport = FakeMaxTransport(self.source)
        self.store = MaxConversationStore(self.database, self.clock)
        self.app = MaxApplication(
            self.settings, self.database, self.demo, self.transport, self.store
        )
        self.sequence = 0

    def event(self, event_type, *, text=None, image_url=None, action=None):
        self.sequence += 1
        key = (
            f"callback:cb-{self.sequence}"
            if event_type == "message_callback"
            else f"message:mid-{self.sequence}"
            if event_type == "message_created"
            else f"start:c1:u1:{self.sequence}"
        )
        return MaxIncomingEvent(
            event_type, key, "u1", "c1", self.sequence,
            message_id=f"mid-{self.sequence}" if event_type != "bot_started" else None,
            text=text,
            image_url=image_url,
            callback_id=f"cb-{self.sequence}" if event_type == "message_callback" else None,
            callback_payload=action,
        )

    def callback(self, action):
        event = self.event("message_callback", action=action)
        self.app.handle(event)
        return event

    def onboard_to_prompt(self) -> None:
        self.app.handle(self.event("bot_started"))
        self.assertEqual(self.store.get("u1").state, "legal_required")
        self.callback("legal:accept_all")
        menu = self.transport.messages[-1]
        self.assertEqual(menu[2][0].text, "💬 Своя идея")
        self.assertEqual(len(menu[2]), 9)
        self.callback("custom")
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/source")
        )
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")

    def generate_first(self) -> None:
        self.onboard_to_prompt()
        self.app.handle(self.event("message_created", text="Сделай светлый фон"))
        self.assertEqual(self.store.get("u1").state, "confirmation")
        confirmation = self.transport.messages[-1]
        self.assertIn("Я понял задачу так", confirmation[1])
        self.assertIn("Бесплатных вариантов доступно: 5", confirmation[1])
        self.callback("prompt:start")
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.store.get("u1").state, "result_ready")

    def test_start_legal_upload_prompt_cancel_and_invalid_file(self) -> None:
        self.app.handle(self.event("message_created", text="/start"))
        self.assertEqual(self.store.get("u1").state, "legal_required")
        self.callback("legal:offer")
        self.assertIn("Оферта (черновик)", self.transport.messages[-1][1])
        self.callback("legal:privacy")
        self.assertIn("Обработка данных (черновик)", self.transport.messages[-1][1])
        self.callback("legal:accept_all")
        self.callback("custom")

        invalid = self.base / "invalid.bin"
        invalid.write_text("not image", encoding="utf-8")
        self.transport.source = invalid
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/invalid")
        )
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        self.assertEqual(self.provider.calls, 0)

        self.transport.source = self.source
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/source")
        )
        self.app.handle(self.event("message_created", text="Измени фон"))
        self.callback("prompt:cancel")
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.store.get("u1").state, "main_menu")

    def test_processing_preview_original_guard_duplicate_and_unlock_placeholder(self) -> None:
        self.generate_first()
        self.assertTrue(any("Обрабатываю фотографию" in row[1] for row in self.transport.messages))
        self.assertTrue(self.transport.edits)
        delivered_path = self.transport.images[-1][1]
        self.assertTrue(delivered_path.is_file())
        self.assertIn("Бесплатных вариантов осталось: 4", self.transport.images[-1][2])
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT original_result_path,demo_result_path FROM generation_attempts"
            ).fetchone()
        self.assertEqual(delivered_path, Path(attempt["demo_result_path"]))
        self.assertNotEqual(delivered_path, Path(attempt["original_result_path"]))

        duplicate = self.event("message_created", text="ignored")
        self.assertTrue(self.app.handle(duplicate))
        message_count = len(self.transport.messages)
        self.assertFalse(self.app.handle(duplicate))
        self.assertEqual(len(self.transport.messages), message_count)

        unlock_event = self.callback("result:unlock")
        self.assertEqual(self.transport.messages[-1][1], UNLOCK_PLACEHOLDER)
        self.assertNotIn(str(attempt["original_result_path"]), self.transport.messages[-1][1])
        callback_message_count = len(self.transport.messages)
        self.assertFalse(self.app.handle(unlock_event))
        self.assertEqual(len(self.transport.messages), callback_message_count)

    def test_correction_repeat_gallery_navigation_favorite_and_physical_delete(self) -> None:
        self.generate_first()
        first = self.store.get("u1").current_version_id
        self.callback("result:correct")
        self.app.handle(self.event("message_created", text="Сделай лицо естественнее"))
        self.clock.advance(2)
        self.callback("prompt:start")
        second = self.store.get("u1").current_version_id
        self.clock.advance(2)
        self.callback("result:repeat")
        dialog = self.store.get("u1")
        third = dialog.current_version_id
        with self.database.read() as connection:
            versions = connection.execute(
                """SELECT id,parent_version_id,correction_prompt,effective_prompt
                   FROM gallery_versions ORDER BY version_number"""
            ).fetchall()
            session = connection.execute("SELECT successful_generations FROM demo_sessions").fetchone()
            root = Path(connection.execute(
                "SELECT storage_root_path FROM gallery_items WHERE id=?",
                (dialog.current_gallery_item_id,),
            ).fetchone()[0])
        self.assertEqual([row["id"] for row in versions], [first, second, third])
        self.assertEqual(versions[1]["parent_version_id"], first)
        self.assertEqual(versions[1]["correction_prompt"], "Сделай лицо естественнее")
        self.assertEqual(versions[2]["parent_version_id"], second)
        self.assertEqual(versions[2]["effective_prompt"], versions[1]["effective_prompt"])
        self.assertEqual(session["successful_generations"], 3)

        self.callback("result:favorite")
        self.callback("studio:works")
        self.assertIn("📁 Мои работы", self.transport.messages[-1][1])
        self.callback(f"works:open:{dialog.current_gallery_item_id}")
        self.callback("work:previous")
        self.callback("work:main")
        self.assertTrue(self.transport.images)

        self.callback("result:delete")
        self.assertIn("Удалить эту работу", self.transport.messages[-1][1])
        self.callback("delete:confirm")
        self.assertFalse(root.exists())
        with self.database.read() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM gallery_items").fetchone()[0], 0)
            cleared = connection.execute(
                "SELECT source_file_path,status FROM demo_sessions"
            ).fetchone()
        self.assertEqual(cleared["source_file_path"], "")
        self.assertEqual(cleared["status"], "deleted")
        self.assertEqual(self.store.get("u1").state, "deleted")

    def test_send_failure_and_technical_failure_do_not_debit(self) -> None:
        self.onboard_to_prompt()
        self.app.handle(self.event("message_created", text="Измени фон"))
        self.transport.image_delivery = False
        self.callback("prompt:start")
        with self.database.read() as connection:
            session = connection.execute(
                "SELECT successful_generations FROM demo_sessions"
            ).fetchone()
            attempt = connection.execute(
                "SELECT status FROM generation_attempts"
            ).fetchone()
        self.assertEqual(session[0], 0)
        self.assertEqual(attempt[0], "delivery_failed")
        self.assertEqual(self.store.get("u1").state, "confirmation")

        self.provider.fail = RuntimeError("provider unavailable")
        self.transport.image_delivery = True
        self.clock.advance(2)
        technical = self.event("message_callback", action="prompt:start")
        with self.assertRaises(RuntimeError):
            self.app.handle(technical)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT successful_generations FROM demo_sessions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM generation_attempts ORDER BY created_at DESC LIMIT 1"
                ).fetchone()[0],
                "failed_technical",
            )
        self.assertEqual(self.store.get("u1").state, "confirmation")

    def test_transport_error_marks_event_retryable(self) -> None:
        self.app.handle(self.event("bot_started"))
        self.callback("legal:accept_all")
        self.transport.fail_next_message = True
        event = self.event("message_callback", action="custom")
        with self.assertRaises(MaxTransportError):
            self.app.handle(event)
        self.assertTrue(self.store.begin_event(event.event_key, event.event_type))
