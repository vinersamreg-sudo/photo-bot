from pathlib import Path
from unittest import TestCase

from app.config import Settings
from app.gemini_image_provider import GeminiConfigurationError, GeminiImageProvider
from app.openai_client import OpenAIConfigurationError
from app.provider_router import select_image_provider


class ProviderRouterTests(TestCase):
    def test_gemini_and_nanobanana_use_confirmed_models_and_labels(self) -> None:
        client = object()
        settings = Settings(
            "",
            "gpt-image-2",
            "test",
            Path.cwd(),
            gemini_image_model="gemini-3.1-flash-image",
            nanobanana_image_model="gemini-3-pro-image",
        )

        gemini = select_image_provider(
            settings, provider_name="gemini", gemini_client=client
        ).provider
        nano = select_image_provider(
            settings, provider_name="nanobanana", gemini_client=client
        ).provider

        self.assertIsInstance(gemini, GeminiImageProvider)
        self.assertEqual((gemini.name, gemini.model), ("gemini", "gemini-3.1-flash-image"))
        self.assertEqual((nano.name, nano.model), ("nanobanana", "gemini-3-pro-image"))

    def test_repository_default_selects_gemini_nano_banana_2(self) -> None:
        settings = Settings("", "gpt-image-2", "test", Path.cwd())

        selected = select_image_provider(settings, gemini_client=object()).provider

        self.assertIsInstance(selected, GeminiImageProvider)
        self.assertEqual(
            (selected.name, selected.model),
            ("gemini", "gemini-3.1-flash-image"),
        )

    def test_missing_google_credentials_fail_before_provider_request(self) -> None:
        settings = Settings(
            "", "gpt-image-2", "test", Path.cwd(), image_provider="gemini"
        )
        with self.assertRaisesRegex(GeminiConfigurationError, "GEMINI_API_KEY"):
            select_image_provider(settings)

    def test_missing_openai_model_fails_before_provider_request(self) -> None:
        settings = Settings(
            "test-key", "", "test", Path.cwd(), image_provider="openai"
        )
        with self.assertRaisesRegex(OpenAIConfigurationError, "OPENAI_IMAGE_MODEL"):
            select_image_provider(settings, openai_client=object())

    def test_openai_remains_an_explicit_provider_option(self) -> None:
        settings = Settings(
            "test-key",
            "gpt-image-2",
            "test",
            Path.cwd(),
            image_provider="openai",
        )

        selected = select_image_provider(
            settings, openai_client=object()
        ).provider

        self.assertEqual((selected.name, selected.model), ("openai", "gpt-image-2"))
