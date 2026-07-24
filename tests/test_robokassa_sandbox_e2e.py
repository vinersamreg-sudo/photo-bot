import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from scripts.set_robokassa_test_secrets import NAMES, update_env


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "robokassa-owner-sandbox.yml"
HARNESS = ROOT / "scripts" / "robokassa_sandbox_e2e.py"


class RobokassaSandboxE2ETests(TestCase):
    def test_secret_installer_updates_only_runtime_names_and_clears_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "KEEP=value\nROBOKASSA_PASSWORD1=old\n", encoding="utf-8"
            )
            values = ("merchant-test", "test-password-one", "test-password-two")
            update_env(env_file, values)
            configured = env_file.read_text(encoding="utf-8")
            self.assertIn("KEEP=value\n", configured)
            for name, value in zip(NAMES, values):
                self.assertEqual(configured.count(f"{name}="), 1)
                self.assertIn(f"{name}={value}\n", configured)
            if os.name != "nt":
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
            update_env(env_file, None)
            cleared = env_file.read_text(encoding="utf-8")
            self.assertEqual(cleared, "KEEP=value\n")
            self.assertTrue(all(f"{name}=" not in cleared for name in NAMES))

    def test_workflow_is_one_order_owner_only_and_always_restores_fail_closed(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("environment: production", workflow)
        self.assertIn("group: photo-bot-production", workflow)
        self.assertIn("MAX_POLL_OBSERVE_ONLY false", workflow)
        self.assertIn("settings.max_allowed_user_ids == settings.max_owner_user_ids", workflow)
        self.assertIn("set_env PILOT_USER_LIMIT 0", workflow)
        self.assertIn("set_env PAYMENT_REFUNDS_ENABLED false", workflow)
        self.assertIn("set_env ROBOKASSA_MODE sandbox", workflow)
        self.assertIn("set_env ROBOKASSA_PRODUCTION_APPROVED false", workflow)
        self.assertIn("set_env ROBOKASSA_HASH_ALGORITHM sha256", workflow)
        self.assertIn("set_env ROBOKASSA_SANDBOX_DUPLICATE_PROBE true", workflow)
        self.assertIn("wait-order", workflow)
        self.assertIn("wait-paid", workflow)
        self.assertEqual(workflow.count("ServerAliveInterval=20"), 3)
        self.assertEqual(workflow.count("ServerAliveCountMax=6"), 3)
        self.assertIn("if: always()", workflow)
        self.assertGreaterEqual(workflow.count("MAX_POLL_OBSERVE_ONLY true"), 1)
        self.assertGreaterEqual(workflow.count("set_env PAYMENTS_ENABLED false"), 2)
        self.assertGreaterEqual(workflow.count("set_env PAYMENT_PROVIDER disabled"), 2)
        self.assertGreaterEqual(workflow.count("set_env PAYMENT_WEBHOOK_ENABLED false"), 2)
        self.assertGreaterEqual(
            workflow.count("set_env ROBOKASSA_SANDBOX_DUPLICATE_PROBE false"), 2
        )
        self.assertIn("--clear", workflow)
        self.assertIn('test "$GET_STATUS" = 405', workflow)
        self.assertIn('test "$POST_STATUS" = 503', workflow)
        self.assertIn("runtime_count=1", workflow)

    def test_workflow_maps_only_test_credentials_without_command_arguments(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("secrets.ROBOKASSA_TEST_PASSWORD_1", workflow)
        self.assertIn("secrets.ROBOKASSA_TEST_PASSWORD_2", workflow)
        self.assertNotIn("secrets.ROBOKASSA_PASSWORD1", workflow)
        self.assertNotIn("secrets.ROBOKASSA_PASSWORD2", workflow)
        self.assertIn("scripts.set_robokassa_test_secrets", workflow)
        self.assertIn('printf \'%s\\n\' "$TEST_PASSWORD_1"', workflow)
        self.assertIn('printf \'%s\\n\' "$TEST_PASSWORD_2"', workflow)
        self.assertNotIn("--password", workflow)

    def test_evidence_harness_has_no_generation_or_refund_execution_path(self) -> None:
        harness = HARNESS.read_text(encoding="utf-8")
        self.assertNotIn("images.edit", harness)
        self.assertNotIn("submit_refund", harness)
        self.assertNotIn("prepare_refund", harness)
        self.assertNotIn("generate(", harness)
        self.assertIn("orphan_files", harness)
        self.assertIn("quick_check", harness)
        self.assertIn("generation_attempts", harness)
        self.assertIn("payment_receipts", harness)
