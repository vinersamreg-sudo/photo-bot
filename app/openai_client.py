"""Small, testable OpenAI client boundary."""

from __future__ import annotations

import logging
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

from app.config import Settings


LOGGER = logging.getLogger(__name__)


class OpenAIConfigurationError(RuntimeError):
    """Raised when OpenAI configuration is incomplete."""


class OpenAICheckError(RuntimeError):
    """Raised when the OpenAI authorization check fails."""


class OpenAIModelError(OpenAICheckError):
    """Raised when the configured image model is unavailable to the account."""


class OpenAIQuotaError(OpenAICheckError):
    """Raised when OpenAI reports insufficient account quota."""


def safe_api_error_details(exc: Any) -> tuple[str, str, str]:
    """Return non-secret provider diagnostics suitable for logs."""

    return (
        str(getattr(exc, "code", None) or "unknown"),
        str(getattr(exc, "type", None) or "unknown"),
        str(getattr(exc, "request_id", None) or "unknown"),
    )


def create_openai_client(settings: Settings) -> OpenAI:
    """Create an OpenAI client, rejecting an absent API key explicitly."""

    if not settings.openai_api_key:
        raise OpenAIConfigurationError(
            "OPENAI_API_KEY is not configured. Add it to the local .env file."
        )
    return OpenAI(api_key=settings.openai_api_key)


def check_openai_connection(client: Any, image_model: str) -> None:
    """Check authorization and model visibility without generating content."""

    if not image_model:
        raise OpenAIConfigurationError(
            "OPENAI_IMAGE_MODEL is not configured. Add it to the .env file."
        )

    try:
        models = client.models.list()
    except AuthenticationError as exc:
        LOGGER.error("OpenAI rejected the configured credentials")
        raise OpenAICheckError("OpenAI authentication failed") from exc
    except APIConnectionError as exc:
        LOGGER.error("Could not connect to the OpenAI API")
        raise OpenAICheckError("Could not connect to the OpenAI API") from exc
    except RateLimitError as exc:
        if getattr(exc, "code", None) == "insufficient_quota":
            LOGGER.error("OpenAI account has insufficient quota")
            raise OpenAIQuotaError("OpenAI account has insufficient quota") from exc
        LOGGER.error("OpenAI API rate limit was reached")
        raise OpenAICheckError("OpenAI API rate limit was reached") from exc
    except APIStatusError as exc:
        code, error_type, request_id = safe_api_error_details(exc)
        LOGGER.error(
            "OpenAI API check failed (status=%s, code=%s, type=%s, request_id=%s)",
            exc.status_code,
            code,
            error_type,
            request_id,
        )
        raise OpenAICheckError(
            "OpenAI API check failed: "
            f"HTTP {exc.status_code}, code={code}, type={error_type}"
        ) from exc

    available_models = {model.id for model in models.data}
    if image_model not in available_models:
        LOGGER.error("Configured OpenAI image model is unavailable: %s", image_model)
        raise OpenAIModelError(
            f"Configured OpenAI image model is unavailable: {image_model}"
        )

    LOGGER.info("OpenAI authorization and model availability check succeeded")
