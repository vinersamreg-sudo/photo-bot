import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase

from scripts.openai_balance_confirm import update_confirmation
from scripts.runtime_state_guard import restore, snapshot, verify


class ReadinessControlTests(TestCase):
    def test_runtime_state_guard_restores_exact_file_and_deletes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            snapshot_file = root / "runtime-state.env"
            original = (
                "MAX_POLL_OBSERVE_ONLY=false\n"
                "PAYMENTS_ENABLED=true\n"
                "PAYMENT_PROVIDER=robokassa\n"
                "PILOT_USER_LIMIT=5\n"
                "MAX_PILOT_USER_IDS=private-one,private-two\n"
                "OPENAI_IMAGE_REQUESTS_ENABLED=true\n"
                "UNRELATED_SECRET=private-value\n"
            )
            env_file.write_text(original, encoding="utf-8")
            snapshot(env_file, snapshot_file)
            env_file.write_text(
                "MAX_POLL_OBSERVE_ONLY=true\n"
                "PAYMENTS_ENABLED=false\n"
                "PAYMENT_PROVIDER=disabled\n"
                "PILOT_USER_LIMIT=0\n",
                encoding="utf-8",
            )
            mismatch = verify(env_file, snapshot_file)
            self.assertFalse(mismatch["matches"])
            self.assertIn("PAYMENTS_ENABLED", mismatch["mismatched_keys"])
            self.assertNotIn("private-value", json.dumps(mismatch))
            restore(env_file, snapshot_file)
            self.assertEqual(env_file.read_text(encoding="utf-8"), original)
            matched = verify(
                env_file, snapshot_file, delete_on_success=True
            )
            self.assertTrue(matched["matches"])
            self.assertTrue(matched["snapshot_deleted"])
            self.assertFalse(snapshot_file.exists())
            if os.name != "nt":
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_openai_balance_confirmation_is_manual_atomic_and_preserves_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "KEEP=value\nOPENAI_BALANCE_USD=99\n", encoding="utf-8"
            )
            update_confirmation(
                env_file, "4.17", "2026-07-27T10:00:00+04:00"
            )
            content = env_file.read_text(encoding="utf-8")
            self.assertIn("KEEP=value\n", content)
            self.assertEqual(content.count("OPENAI_BALANCE_USD="), 1)
            self.assertIn("OPENAI_BALANCE_USD=4.17\n", content)
            self.assertIn(
                "OPENAI_BALANCE_CONFIRMED_AT=2026-07-27T06:00:00+00:00\n",
                content,
            )
            with self.assertRaisesRegex(ValueError, "non-negative"):
                update_confirmation(
                    env_file, "-1", "2026-07-27T10:00:00+00:00"
                )
