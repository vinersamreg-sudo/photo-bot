import logging
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from app.config import Settings
from app.main import SecretRedactionFilter
from app.openai_client import (
    OpenAIConfigurationError,
    OpenAIModelError,
    check_openai_connection,
    create_openai_client,
)


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

    def test_connection_check_confirms_configured_model(self) -> None:
        client = SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: SimpleNamespace(
                    data=[SimpleNamespace(id="gpt-image-2")]
                )
            )
        )

        check_openai_connection(client, "gpt-image-2")

    def test_connection_check_rejects_unavailable_model(self) -> None:
        client = SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: SimpleNamespace(data=[SimpleNamespace(id="other-model")])
            )
        )

        with self.assertRaisesRegex(OpenAIModelError, "unavailable"):
            check_openai_connection(client, "gpt-image-2")

    def test_connection_check_requires_model_name(self) -> None:
        client = SimpleNamespace(models=SimpleNamespace(list=lambda: None))
        with self.assertRaisesRegex(OpenAIConfigurationError, "OPENAI_IMAGE_MODEL"):
            check_openai_connection(client, "")
