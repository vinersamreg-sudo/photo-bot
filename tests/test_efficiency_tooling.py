import tempfile
from pathlib import Path
from unittest import TestCase

from scripts.production_status import FLAG_KEYS, _read_env
from scripts.runtime_state_guard import PROTECTED_KEYS
from scripts.test_fast import PROFILES


class EfficiencyToolingTests(TestCase):
    def test_fast_profiles_cover_required_domains_without_full_discovery(self) -> None:
        self.assertEqual(
            set(PROFILES), {"max", "payments", "openai", "site", "storage"}
        )
        for targets in PROFILES.values():
            self.assertTrue(targets)
            self.assertNotIn("discover", targets)

    def test_runtime_guard_protects_public_commercial_baseline(self) -> None:
        for key in (
            "MAX_PUBLIC_ACCESS_ENABLED",
            "MAX_POLL_OBSERVE_ONLY",
            "MAX_TRANSPORT_MODE",
            "PAYMENTS_ENABLED",
            "PAYMENT_PROVIDER",
            "PAYMENT_WEBHOOK_ENABLED",
            "ROBOKASSA_MODE",
            "ROBOKASSA_PRODUCTION_APPROVED",
            "OPENAI_IMAGE_REQUESTS_ENABLED",
        ):
            self.assertIn(key, PROTECTED_KEYS)

    def test_production_status_reads_only_allowlisted_non_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "MAX_PUBLIC_ACCESS_ENABLED=true\n"
                "PAYMENTS_ENABLED=true\n"
                "PAYMENT_PROVIDER=robokassa\n"
                "OPENAI_API_KEY=private\n"
                "ROBOKASSA_PASSWORD_1=private\n",
                encoding="utf-8",
            )
            values = _read_env(env_file)
        self.assertEqual(values["MAX_PUBLIC_ACCESS_ENABLED"], "true")
        self.assertEqual(values["PAYMENTS_ENABLED"], "true")
        self.assertEqual(values["PAYMENT_PROVIDER"], "robokassa")
        self.assertNotIn("OPENAI_API_KEY", values)
        self.assertNotIn("ROBOKASSA_PASSWORD_1", values)
        self.assertIn("MAX_PUBLIC_ACCESS_ENABLED", FLAG_KEYS)
