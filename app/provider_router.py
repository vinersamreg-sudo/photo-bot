"""Configuration-driven image provider selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.config import Settings
from app.gemini_image_provider import GeminiImageProvider, create_gemini_client
from app.image_provider import (
    ContextAwareImageProvider,
    FakeImageProvider,
    ImageProvider,
    OpenAIImageProvider,
    OpenAIResponsesImageProvider,
)
from app.openai_client import OpenAIConfigurationError, create_openai_client
from app.provider_context import OpenAIProviderContextGateway


@dataclass(frozen=True)
class ProviderSelection:
    provider: ImageProvider
    context_gateway: Optional[OpenAIProviderContextGateway]


def select_image_provider(
    settings: Settings,
    *,
    provider_name: str | None = None,
    openai_client: Any = None,
    gemini_client: Any = None,
) -> ProviderSelection:
    """Build exactly the configured provider or fail before any request."""

    selected = (provider_name or settings.image_provider).strip().lower()
    if selected == "fake":
        return ProviderSelection(FakeImageProvider(), None)
    if selected == "openai":
        if not settings.openai_image_model:
            raise OpenAIConfigurationError(
                "OPENAI_IMAGE_MODEL is not configured. Add it to the local .env file."
            )
        client = openai_client or create_openai_client(settings)
        stateless = OpenAIImageProvider(
            client,
            settings.openai_image_model,
            quality=settings.image_edit_quality,
            size=settings.image_edit_size,
            input_fidelity=settings.image_edit_input_fidelity,
            output_format=settings.image_edit_output_format,
        )
        contextual = OpenAIResponsesImageProvider(
            client,
            settings.openai_responses_model,
            settings.openai_image_model,
            quality=settings.image_edit_quality,
            size=settings.image_edit_size,
            output_format=settings.image_edit_output_format,
        )
        return ProviderSelection(
            ContextAwareImageProvider(stateless, contextual),
            OpenAIProviderContextGateway(client),
        )
    if selected in {"gemini", "nanobanana"}:
        client = gemini_client or create_gemini_client(
            settings.gemini_api_key, settings.generation_timeout_seconds
        )
        model = (
            settings.gemini_image_model
            if selected == "gemini"
            else settings.nanobanana_image_model
        )
        return ProviderSelection(
            GeminiImageProvider(
                client,
                model,
                provider_name=selected,
                size=settings.image_edit_size,
                output_format=settings.image_edit_output_format,
            ),
            None,
        )
    raise ValueError("provider must be openai, gemini, nanobanana or fake")
