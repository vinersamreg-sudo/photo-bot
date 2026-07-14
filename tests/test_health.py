import tempfile
from pathlib import Path
from unittest import TestCase

from app.config import Settings
from app.main import health_errors


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
