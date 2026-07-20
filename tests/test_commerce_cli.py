import argparse
import contextlib
import io
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase

from app.config import Settings
from app.database import Database
from app.main import run_commerce_adjust, run_credit_report


class CommerceCliTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings("test", "gpt-image-2", "test", self.root)
        self.database = Database(self.settings.database_path)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES('user-1','max',?,?)",
                ("sensitive-platform-id", datetime.now(timezone.utc).isoformat()),
            )

    @staticmethod
    def _args(**overrides):
        values = {
            "platform_user_id": "sensitive-platform-id",
            "delta": 1,
            "reason": "support-ticket-42",
            "idempotency_key": None,
            "apply": False,
            "format": "json",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_adjust_is_dry_run_by_default_and_masks_user(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run_commerce_adjust(
                self.settings, self._args(), entity="generation_credit"
            )
        payload = json.loads(output.getvalue())
        self.assertEqual((code, payload["mode"], payload["database_mutated"]), (0, "dry-run", False))
        self.assertNotIn("sensitive-platform-id", output.getvalue())
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT available_generation_credits FROM user_credit_accounts WHERE user_id='user-1'"
                ).fetchone()[0],
                2,
            )

    def test_apply_requires_idempotency_key_and_writes_audit(self) -> None:
        self.assertEqual(
            run_commerce_adjust(
                self.settings, self._args(apply=True), entity="generation_credit"
            ),
            2,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run_commerce_adjust(
                self.settings,
                self._args(apply=True, idempotency_key="support-42"),
                entity="generation_credit",
            )
        self.assertEqual(code, 0)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM commerce_admin_audit").fetchone()[0],
                1,
            )

    def test_empty_reason_is_rejected_and_report_is_privacy_safe(self) -> None:
        self.assertEqual(
            run_commerce_adjust(
                self.settings, self._args(reason="   "), entity="unlock_entitlement"
            ),
            2,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                run_credit_report(
                    self.settings,
                    "sensitive-platform-id",
                    history=True,
                    output_format="json",
                ),
                0,
            )
        self.assertNotIn("sensitive-platform-id", output.getvalue())
