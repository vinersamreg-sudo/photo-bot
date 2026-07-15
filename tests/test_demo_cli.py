import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.config import Settings
from app.database import Database
from app.main import run_demo_edit, run_demo_stats, run_gallery_cleanup


class Args:
    provider = "fake"
    scenario = None
    event_id = "cli-test-event"
    prompt = "Сделай нейтральный светлый фон"
    user_id = "test-user"
    image = ""


class DemoCliTests(TestCase):
    def test_fake_vertical_slice_and_stats_do_not_disclose_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ("data", "logs", "temp"):
                (base / name).mkdir()
            settings = Settings("", "fake-image-edit-v1", "test", base, demo_min_request_interval_seconds=1)
            image = base / "input.png"
            Image.new("RGB", (320, 240), "white").save(image)
            args = Args()
            args.image = str(image)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_demo_edit(settings, args), 0)
            report = json.loads(output.getvalue())
            self.assertFalse(report["original_disclosed"])
            self.assertNotIn("original_result", report)
            self.assertTrue(Path(report["demo_preview"]).is_file())

            stats_output = io.StringIO()
            with redirect_stdout(stats_output):
                self.assertEqual(run_demo_stats(settings), 0)
            stats = json.loads(stats_output.getvalue())
            self.assertEqual(stats["users"], 1)
            self.assertEqual(stats["successful_demo_results"], 1)
            self.assertEqual(stats["delivery_failures"], 0)

            with Database(settings.database_path).read() as connection:
                row = connection.execute(
                    "SELECT original_result_path,demo_result_path FROM generation_attempts"
                ).fetchone()
            self.assertNotEqual(row[0], row[1])
            self.assertTrue(Path(row[0]).is_file())

            cleanup_output = io.StringIO()
            with redirect_stdout(cleanup_output):
                self.assertEqual(run_gallery_cleanup(settings, execute=False), 0)
            cleanup = json.loads(cleanup_output.getvalue())
            self.assertEqual(cleanup, {"mode": "dry-run", "count": 0, "gallery_item_ids": []})
