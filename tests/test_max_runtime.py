import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.config import Settings
from app.database import Database
from app.max_conversation import MaxConversationStore
from app.max_runtime import run_polling


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
