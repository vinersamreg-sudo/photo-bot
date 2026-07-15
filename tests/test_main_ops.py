import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.config import Settings
from app.main import run_max_check, run_process
from app.max_transport import MaxTransportError


class FakeMaxClient:
    last_status_code = 200

    def __init__(self, bot=None, error=None) -> None:
        self.bot = bot or {"user_id": 42, "username": "pixora"}
        self.error = error
        self.closed = False

    def get_me(self):
        if self.error:
            raise self.error
        return self.bot

    def close(self):
        self.closed = True


class MainOperationalTests(TestCase):
    def settings(self, directory: str, **overrides) -> Settings:
        values = {
            "max_bot_token": "test-token",
            "max_transport_mode": "disabled",
        }
        values.update(overrides)
        return Settings("", "", "test", Path(directory), **values)

    def test_max_check_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeMaxClient()
            with patch("app.max_transport.MaxApiClient", return_value=fake):
                self.assertEqual(run_max_check(self.settings(directory)), 0)
            self.assertTrue(fake.closed)

    def test_max_check_auth_and_network_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("invalid_token", "network"):
                fake = FakeMaxClient(error=MaxTransportError("safe", kind=kind))
                with patch("app.max_transport.MaxApiClient", return_value=fake):
                    self.assertEqual(run_max_check(self.settings(directory)), 1)
                self.assertTrue(fake.closed)

    def test_disabled_mode_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(run_process(self.settings(directory)), 2)

    def test_sigterm_requests_graceful_polling_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handlers = {}

            def register(signum, handler):
                handlers[signum] = handler

            def polling(_settings, stop_event):
                import signal
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                self.assertTrue(stop_event.is_set())
                return 0

            with patch("app.main.signal.signal", side_effect=register):
                with patch("app.max_runtime.run_polling", side_effect=polling):
                    self.assertEqual(
                        run_process(
                            self.settings(directory, max_transport_mode="polling")
                        ),
                        0,
                    )
