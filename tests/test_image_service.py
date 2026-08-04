import tempfile
from pathlib import Path
from unittest import TestCase

from app.config import Settings
from app.gemini_image_provider import GeminiImageProvider
from app.image_service import build_demo_service


class ImageServiceTests(TestCase):
    def test_default_gemini_provider_is_wired_without_external_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ("data", "logs", "temp"):
                (base / name).mkdir()
            settings = Settings(
                "",
                "gpt-image-2",
                "test",
                base,
            )

            service = build_demo_service(settings, gemini_client=object())

            self.assertIsInstance(service.provider, GeminiImageProvider)
            self.assertEqual(service.provider.name, "gemini")
            self.assertEqual(service.provider.model, "gemini-3-pro-image")
            self.assertIsNone(service.provider_context_service.gateway)
