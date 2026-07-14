"""Small, testable OpenAI client boundary."""

from __future__ import annotations

import logging
from typing import Any

from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI

from app.config import Settings


LOGGER = logging.getLogger(__name__)


class OpenAIConfigurationError(RuntimeError):
    """Raised when OpenAI configuration is incomplete."""


class OpenAICheckError(RuntimeError):
    """Raised when the OpenAI authorization check fails."""


def create_openai_client(settings: Settings) -> OpenAI:
    """Create an OpenAI client, rejecting an absent API key explicitly."""

    if not settings.openai_api_key:
        raise OpenAIConfigurationError(
            "OPENAI_API_KEY is not configured. Add it to the local .env file."
        )
    return OpenAI(api_key=settings.openai_api_key)


def check_openai_connection(client: Any) -> None:
    """Make a minimal authenticated request without generating paid content."""

    try:
        client.models.list()
    except AuthenticationError as exc:
        LOGGER.error("OpenAI rejected the configured credentials")
        raise OpenAICheckError("OpenAI authentication failed") from exc
    except APIConnectionError as exc:
        LOGGER.error("Could not connect to the OpenAI API")
        raise OpenAICheckError("Could not connect to the OpenAI API") from exc
    except APIStatusError as exc:
        LOGGER.error("OpenAI API check failed with HTTP status %s", exc.status_code)
        raise OpenAICheckError(
            f"OpenAI API check failed with HTTP status {exc.status_code}"
        ) from exc

    LOGGER.info("OpenAI authorization check succeeded")
