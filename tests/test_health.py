import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path
from unittest import TestCase

from app.config import Settings
from app.database import Database
from app.main import health_errors
from app.max_conversation import MaxConversationStore
from app.max_transport import SingleInstanceLock


class HealthcheckTests(TestCase):
    def test_required_directories_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            for name in ("data", "logs", "temp"):
                (base_dir / name).mkdir()
            settings = Settings("", "", "test", base_dir)
            self.assertEqual(health_errors(settings), [])

    def test_missing_directory_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            (base_dir / "data").mkdir()
            (base_dir / "logs").mkdir()
            settings = Settings("", "", "test", base_dir)
            errors = health_errors(settings)
            self.assertTrue(any("temp" in error for error in errors))

    def test_python_older_than_312_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            for name in ("data", "logs", "temp"):
                (base_dir / name).mkdir()
            settings = Settings("", "", "test", base_dir)
            with patch("app.main.sys.version_info", (3, 11, 9)):
                errors = health_errors(settings)
            self.assertTrue(any("Python 3.12" in error for error in errors))

    def test_polling_health_requires_token_database_service_and_contact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            for name in ("data", "logs", "temp"):
                (base_dir / name).mkdir()
            settings = Settings(
                "", "", "production", base_dir, max_transport_mode="polling"
            )
            errors = health_errors(settings)
            self.assertTrue(any("MAX_BOT_TOKEN" in error for error in errors))
            self.assertTrue(any("database" in error.lower() for error in errors))
            self.assertTrue(any("service" in error for error in errors))

    def test_active_polling_health_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            for name in ("data", "logs", "temp"):
                (base_dir / name).mkdir()
            settings = Settings(
                "", "", "production", base_dir,
                max_bot_token="test-token", max_transport_mode="polling",
            )
            store = MaxConversationStore(Database(settings.database_path))
            store.touch_poll_success()
            with SingleInstanceLock(settings.max_poll_lock_path):
                with patch("app.main._systemd_runtime_status", return_value=(True, 123)):
                    with patch("app.main.Path.read_bytes", return_value=b"python -m app.main run"):
                        self.assertEqual(health_errors(settings), [])

    def test_stale_polling_contact_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            for name in ("data", "logs", "temp"):
                (base_dir / name).mkdir()
            settings = Settings(
                "", "", "test", base_dir,
                max_bot_token="test-token", max_transport_mode="polling",
            )
            Database(settings.database_path)
            stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            connection = sqlite3.connect(settings.database_path)
            try:
                connection.execute(
                    "INSERT INTO max_transport_state(name,value,updated_at) VALUES(?,?,?)",
                    ("poll_last_success", "ok", stale),
                )
                connection.commit()
            finally:
                connection.close()
            errors = health_errors(settings)
            self.assertTrue(any("stale" in error for error in errors))
