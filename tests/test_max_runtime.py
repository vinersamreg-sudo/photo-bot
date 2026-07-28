import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.config import Settings
from app.database import Database
from app.max_conversation import MaxConversationStore
from app.max_runtime import OBSERVE_ONLY_TEXT, run_polling
from app.max_transport import MaxTransportError


class FakePollingClient:
    last_status_code = 200

    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self.closed = False
        self.calls = 0

    def get_updates(self, marker, *, timeout):
        self.calls += 1
        self.stop_event.set()
        return [], 77

    def close(self) -> None:
        self.closed = True


class FakeObserveOnlyClient(FakePollingClient):
    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(stop_event)
        self.messages = []
        self.callbacks = []
        self.edits = []

    def get_updates(self, marker, *, timeout):
        self.calls += 1
        self.stop_event.set()
        return [{
            "update_type": "message_callback",
            "timestamp": 10,
            "callback": {
                "callback_id": "cb-observe",
                "payload": "result:correct",
                "user": {"user_id": "owner"},
            },
            "message": {
                "recipient": {"chat_id": "chat"},
                "body": {"mid": "old-message", "text": "Готово"},
            },
        }], 78

    def answer_callback(self, callback_id, notification):
        self.callbacks.append((callback_id, notification))

    def edit_message(self, message_id, text, buttons=()):
        self.edits.append((message_id, text, tuple(buttons)))

    def send_message(self, user_id, text, buttons=()):
        self.messages.append((user_id, text, tuple(buttons)))
        return "observe-reply"


class MaxRuntimeTests(TestCase):
    def test_observe_only_polling_updates_health_without_user_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ("data", "logs", "temp"):
                (base / name).mkdir()
            settings = Settings(
                "", "", "test", base,
                max_bot_token="test-token", max_transport_mode="polling",
                max_poll_observe_only=True,
            )
            stop_event = threading.Event()
            fake = FakePollingClient(stop_event)
            with patch("app.max_runtime.MaxApiClient", return_value=fake):
                self.assertEqual(run_polling(settings, stop_event), 0)
            store = MaxConversationStore(Database(settings.database_path))
            self.assertEqual(store.get_marker(), 77)
            self.assertEqual(store.transport_state("poll_last_success")[0], "ok")
            self.assertTrue(fake.closed)

    def test_observe_only_returns_visible_unavailable_message_and_disables_button(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ("data", "logs", "temp"):
                (base / name).mkdir()
            settings = Settings(
                "", "", "test", base,
                max_bot_token="test-token", max_transport_mode="polling",
                max_poll_observe_only=True,
            )
            before = MaxConversationStore(Database(settings.database_path))
            before.register_keyboard(
                "owner", "registered-message", "Старая кнопка"
            )
            stop_event = threading.Event()
            fake = FakeObserveOnlyClient(stop_event)
            with patch("app.max_runtime.MaxApiClient", return_value=fake):
                self.assertEqual(run_polling(settings, stop_event), 0)
            self.assertEqual(
                fake.callbacks,
                [("cb-observe", "Сервис временно недоступен")],
            )
            self.assertEqual(
                [edit[0] for edit in fake.edits],
                ["registered-message", "old-message"],
            )
            self.assertTrue(all(not edit[2] for edit in fake.edits))
            self.assertEqual(
                fake.messages,
                [("owner", OBSERVE_ONLY_TEXT, ())],
            )
            store = MaxConversationStore(Database(settings.database_path))
            self.assertEqual(store.get_marker(), 78)
            self.assertEqual(store.active_keyboards("owner"), [])
            self.assertFalse(
                store.begin_event("callback:cb-observe", "message_callback")
            )

    def test_stop_event_exits_without_polling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ("data", "logs", "temp"):
                (base / name).mkdir()
            settings = Settings(
                "", "", "test", base,
                max_bot_token="test-token", max_transport_mode="polling",
            )
            stop_event = threading.Event()
            stop_event.set()
            fake = FakePollingClient(stop_event)
            with patch("app.max_runtime.MaxApiClient", return_value=fake):
                self.assertEqual(run_polling(settings, stop_event), 0)
            self.assertEqual(fake.calls, 0)

    def test_handlers_require_owner_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ("data", "logs", "temp"):
                (base / name).mkdir()
            settings = Settings(
                "", "", "test", base,
                max_bot_token="test-token", max_transport_mode="polling",
                max_poll_observe_only=False,
            )
            fake = FakePollingClient(threading.Event())
            with patch("app.max_runtime.MaxApiClient", return_value=fake):
                with self.assertRaises(MaxTransportError) as raised:
                    run_polling(settings, threading.Event())
            self.assertEqual(raised.exception.kind, "configuration_missing")
            self.assertTrue(fake.closed)
