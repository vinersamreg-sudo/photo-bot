from pathlib import Path
from unittest import TestCase

from app.config import PROJECT_ROOT, load_settings


class SettingsTests(TestCase):
    def test_loads_configuration_from_mapping(self) -> None:
        settings = load_settings(
            environ={
                "OPENAI_API_KEY": "test-key",
                "OPENAI_IMAGE_MODEL": "test-image-model",
                "APP_ENV": "test",
                "BASE_DIR": str(PROJECT_ROOT),
                "MAX_OWNER_USER_IDS": " owner-1,owner-2,owner-1 ",
            }
        )

        self.assertEqual(settings.openai_api_key, "test-key")
        self.assertEqual(settings.openai_image_model, "test-image-model")
        self.assertEqual(settings.app_env, "test")
        self.assertEqual(settings.base_dir, PROJECT_ROOT)
        self.assertEqual(settings.image_edit_quality, "medium")
        self.assertEqual(settings.image_edit_size, "1024x1024")
        self.assertEqual(settings.image_edit_input_fidelity, "auto")
        self.assertEqual(settings.image_edit_output_format, "png")
        self.assertEqual(settings.openai_max_retries, 2)
        self.assertFalse(settings.openai_conversation_memory_enabled)
        self.assertFalse(settings.openai_responses_image_enabled)
        self.assertFalse(settings.openai_conversation_retention_enabled)
        self.assertEqual(settings.openai_context_retention_days, 30)
        self.assertEqual(settings.openai_context_max_idle_days, 14)
        self.assertEqual(settings.openai_context_max_depth, 8)
        self.assertEqual(settings.demo_max_successful_generations, 2)
        self.assertEqual(settings.demo_watermark_text, "ОБРАЗЕЦ")
        self.assertEqual(settings.global_max_concurrent_generations, 2)
        self.assertEqual(settings.demo_retention_days, 30)
        self.assertEqual(settings.paid_retention_days, 180)
        self.assertEqual(settings.trash_retention_days, 30)
        self.assertEqual(settings.max_bot_token, "")
        self.assertEqual(settings.max_api_base_url, "https://platform-api2.max.ru")
        self.assertTrue(settings.max_ca_bundle_path.is_file())
        self.assertEqual(settings.max_transport_mode, "disabled")
        self.assertEqual(settings.max_poll_timeout_seconds, 20)
        self.assertEqual(settings.max_poll_retry_seconds, 5)
        self.assertEqual(settings.max_poll_max_stale_seconds, 90)
        self.assertTrue(settings.max_poll_observe_only)
        self.assertEqual(settings.max_owner_user_ids, ("owner-1", "owner-2"))

    def test_project_root_is_repository_root(self) -> None:
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(PROJECT_ROOT, expected)

    def test_payments_are_fail_closed_and_production_requires_explicit_approval(self) -> None:
        baseline = {
            "APP_ENV": "production",
            "BASE_DIR": str(PROJECT_ROOT),
            "OPENAI_IMAGE_MODEL": "gpt-image-2",
            "PAYMENTS_ENABLED": "true",
            "PAYMENT_PROVIDER": "robokassa",
            "PAYMENT_WEBHOOK_ENABLED": "true",
            "PAYMENT_RESULT_URL": "https://pixora.example/payments/robokassa/result",
            "ROBOKASSA_MERCHANT_LOGIN": "shop",
            "ROBOKASSA_PASSWORD1": "one",
            "ROBOKASSA_PASSWORD2": "two",
            "ROBOKASSA_MODE": "production",
        }
        with self.assertRaisesRegex(ValueError, "explicit"):
            load_settings(environ=baseline)
        settings = load_settings(environ={**baseline, "ROBOKASSA_MODE": "sandbox"})
        self.assertTrue(settings.payments_enabled)
        self.assertEqual(settings.robokassa_mode, "sandbox")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            load_settings(environ={
                **baseline,
                "ROBOKASSA_MODE": "sandbox",
                "PAYMENT_RESULT_URL": "http://pixora.example/result",
            })
        with self.assertRaisesRegex(ValueError, "auth.robokassa.ru"):
            load_settings(environ={
                **baseline,
                "ROBOKASSA_MODE": "sandbox",
                "ROBOKASSA_PAYMENT_URL": "https://payments.invalid/collect",
            })

    def test_refunds_require_payments(self) -> None:
        with self.assertRaisesRegex(ValueError, "while payments are disabled"):
            load_settings(environ={"PAYMENT_REFUNDS_ENABLED": "true"})

    def test_robokassa_commission_cannot_be_negative(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be non-negative"):
            load_settings(environ={"ROBOKASSA_COMMISSION_PERCENT": "-1"})
