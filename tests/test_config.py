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
            }
        )

        self.assertEqual(settings.openai_api_key, "test-key")
        self.assertEqual(settings.openai_image_model, "test-image-model")
        self.assertEqual(settings.app_env, "test")
        self.assertEqual(settings.base_dir, PROJECT_ROOT)
        self.assertEqual(settings.demo_max_successful_generations, 5)
        self.assertEqual(settings.demo_watermark_text, "ОБРАЗЕЦ")
        self.assertEqual(settings.global_max_concurrent_generations, 2)
        self.assertEqual(settings.demo_retention_days, 30)
        self.assertEqual(settings.paid_retention_days, 180)
        self.assertEqual(settings.trash_retention_days, 30)
        self.assertEqual(settings.max_bot_token, "")
        self.assertEqual(settings.max_api_base_url, "https://platform-api2.max.ru")
        self.assertEqual(settings.max_transport_mode, "disabled")
        self.assertEqual(settings.max_poll_timeout_seconds, 30)

    def test_project_root_is_repository_root(self) -> None:
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(PROJECT_ROOT, expected)
