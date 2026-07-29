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
        self.assertFalse(settings.max_public_access_enabled)
        self.assertEqual(settings.max_owner_user_ids, ("owner-1", "owner-2"))
        self.assertFalse(settings.payment_webhook_listener_enabled)

    def test_public_access_is_explicit_and_defaults_closed(self) -> None:
        self.assertFalse(
            load_settings(environ={"APP_ENV": "test"}).max_public_access_enabled
        )
        self.assertTrue(
            load_settings(
                environ={
                    "APP_ENV": "test",
                    "MAX_PUBLIC_ACCESS_ENABLED": "true",
                }
            ).max_public_access_enabled
        )

    def test_project_root_is_repository_root(self) -> None:
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(PROJECT_ROOT, expected)

    def test_manual_openai_balance_guard_is_validated(self) -> None:
        settings = load_settings(
            environ={
                "APP_ENV": "test",
                "OPENAI_BALANCE_USD": "4.17",
                "OPENAI_BALANCE_CONFIRMED_AT": "2026-07-27T10:00:00+00:00",
                "OPENAI_BALANCE_WARNING_USD": "5",
                "OPENAI_BALANCE_CRITICAL_USD": "1",
                "OPENAI_BALANCE_MAX_AGE_HOURS": "48",
                "OPENAI_IMAGE_REQUESTS_ENABLED": "false",
            }
        )
        self.assertEqual(settings.openai_balance_usd, 4.17)
        self.assertFalse(settings.openai_image_requests_enabled)
        self.assertEqual(settings.openai_balance_max_age_hours, 48)
        with self.assertRaisesRegex(ValueError, "ISO-8601"):
            load_settings(
                environ={"OPENAI_BALANCE_CONFIRMED_AT": "not-a-timestamp"}
            )
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            load_settings(
                environ={
                    "OPENAI_BALANCE_WARNING_USD": "1",
                    "OPENAI_BALANCE_CRITICAL_USD": "2",
                }
            )

    def test_payments_are_fail_closed_and_production_requires_explicit_approval(self) -> None:
        baseline = {
            "APP_ENV": "production",
            "BASE_DIR": str(PROJECT_ROOT),
            "OPENAI_IMAGE_MODEL": "gpt-image-2",
            "PAYMENTS_ENABLED": "true",
            "PAYMENT_PROVIDER": "robokassa",
            "PAYMENT_WEBHOOK_LISTENER_ENABLED": "true",
            "PAYMENT_WEBHOOK_ENABLED": "true",
            "PAYMENT_RESULT_URL": "https://ravuna.example/payments/robokassa/result",
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
                "PAYMENT_RESULT_URL": "http://ravuna.example/result",
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

    def test_payment_return_urls_reject_query_parameters(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not contain query parameters"):
            load_settings(environ={
                "PAYMENT_SUCCESS_URL": "https://ravuna.ru/payment-success.html?payment=success",
                "PAYMENT_FAIL_URL": "https://ravuna.ru/payment-failed.html",
            })
        settings = load_settings(environ={
            "PAYMENT_SUCCESS_URL": "https://ravuna.ru/payment-success.html",
            "PAYMENT_FAIL_URL": "https://ravuna.ru/payment-failed.html",
        })
        self.assertEqual(settings.payment_success_url, "https://ravuna.ru/payment-success.html")
        self.assertEqual(settings.payment_fail_url, "https://ravuna.ru/payment-failed.html")

    def test_business_webhook_requires_listener(self) -> None:
        with self.assertRaisesRegex(ValueError, "LISTENER"):
            load_settings(environ={"PAYMENT_WEBHOOK_ENABLED": "true"})
        with self.assertRaisesRegex(ValueError, "PAYMENTS_ENABLED"):
            load_settings(environ={
                "PAYMENT_WEBHOOK_LISTENER_ENABLED": "true",
                "PAYMENT_WEBHOOK_ENABLED": "true",
            })

    def test_permanent_receipt_product_is_validated_without_npd_only_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "permanent Ravuna package"):
            load_settings(environ={"PAYMENT_RECEIPT_ITEM_NAME": "Другая услуга"})
        settings = load_settings(environ={
            "PAYMENT_RECEIPT_ITEM_NAME": "Пакет доступа Ravuna",
            "PAYMENT_RECEIPT_TAX": "none",
        })
        self.assertEqual(settings.payment_receipt_item_name, "Пакет доступа Ravuna")
        self.assertFalse(hasattr(settings, "payment_receipt_payment_method"))
        self.assertFalse(hasattr(settings, "payment_receipt_payment_object"))

    def test_robokassa_commission_cannot_be_negative(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be non-negative"):
            load_settings(environ={"ROBOKASSA_COMMISSION_PERCENT": "-1"})

    def test_robokassa_hash_algorithm_is_sha256_only(self) -> None:
        self.assertEqual(
            load_settings(environ={"ROBOKASSA_HASH_ALGORITHM": "sha256"})
            .robokassa_hash_algorithm,
            "sha256",
        )
        for rejected in ("md5", "sha512"):
            with self.subTest(rejected=rejected):
                with self.assertRaisesRegex(ValueError, "must be sha256"):
                    load_settings(environ={"ROBOKASSA_HASH_ALGORITHM": rejected})

    def test_sandbox_duplicate_probe_is_strictly_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "only in sandbox"):
            load_settings(environ={
                "ROBOKASSA_MODE": "production",
                "ROBOKASSA_SANDBOX_DUPLICATE_PROBE": "true",
            })
        with self.assertRaisesRegex(ValueError, "requires PAYMENT_WEBHOOK_ENABLED"):
            load_settings(environ={
                "ROBOKASSA_MODE": "sandbox",
                "ROBOKASSA_SANDBOX_DUPLICATE_PROBE": "true",
            })
        settings = load_settings(environ={
            "ROBOKASSA_MODE": "sandbox",
            "ROBOKASSA_SANDBOX_DUPLICATE_PROBE": "true",
            "ROBOKASSA_SANDBOX_ORDER_BASELINE": "4",
            "PAYMENTS_ENABLED": "true",
            "PAYMENT_PROVIDER": "robokassa",
            "PAYMENT_WEBHOOK_LISTENER_ENABLED": "true",
            "PAYMENT_WEBHOOK_ENABLED": "true",
            "PAYMENT_RESULT_URL": "https://ravuna.example/payments/robokassa/result",
            "ROBOKASSA_MERCHANT_LOGIN": "shop",
            "ROBOKASSA_PASSWORD1": "one",
            "ROBOKASSA_PASSWORD2": "two",
        })
        self.assertTrue(settings.robokassa_sandbox_duplicate_probe)
        self.assertEqual(settings.robokassa_sandbox_order_baseline, 4)
