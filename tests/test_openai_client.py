import logging
from io import StringIO
from pathlib import Path
from unittest import TestCase

from app.config import Settings
from app.main import SecretRedactionFilter
from app.openai_client import OpenAIConfigurationError, create_openai_client


class OpenAIClientTests(TestCase):
    def test_missing_api_key_has_clear_error(self) -> None:
        settings = Settings("", "", "test", Path.cwd())
        with self.assertRaisesRegex(OpenAIConfigurationError, "OPENAI_API_KEY"):
            create_openai_client(settings)

    def test_secret_is_redacted_from_logs(self) -> None:
        secret = "sk-test-secret-value"
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SecretRedactionFilter([secret]))
        logger = logging.getLogger("photo_bot_secret_test")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        logger.info("credential=%s", secret)

        output = stream.getvalue()
        self.assertNotIn(secret, output)
        self.assertIn("[REDACTED]", output)
