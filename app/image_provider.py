"""Replaceable image-edit provider boundary."""

from __future__ import annotations

import base64
import binascii
import io
import mimetypes
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageDraw
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    OpenAIError,
    RateLimitError,
)

from app.domain import (
    PolicyRejectedError,
    ProviderQuotaError,
    ProviderContextRequest,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


class ImageProvider(Protocol):
    name: str
    model: str

    def edit(self, source_path: Path, prompt: str) -> ProviderResult: ...


def _validate_provider_prompt(prompt: str) -> None:
    if not prompt.strip() or not prompt.isascii():
        raise ValueError("Image providers accept normalized English ASCII prompts only")


def _provider_error_code(exc: OpenAIError) -> str | None:
    direct = getattr(exc, "code", None)
    if direct:
        return str(direct)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        nested = body.get("error")
        if isinstance(nested, dict) and nested.get("code"):
            return str(nested["code"])
        if body.get("code"):
            return str(body["code"])
    return None


class OpenAIImageProvider:
    name = "openai"

    def __init__(
        self,
        client: Any,
        model: str,
        quality: str = "medium",
        size: str = "1024x1024",
        input_fidelity: str = "auto",
        output_format: str = "png",
    ) -> None:
        self.client = client
        self.model = model
        self.quality = quality
        self.size = size
        self.input_fidelity = input_fidelity
        self.output_format = output_format

    def edit(self, source_path: Path, prompt: str) -> ProviderResult:
        _validate_provider_prompt(prompt)
        started = time.monotonic()
        http_status = None
        try:
            with source_path.open("rb") as source:
                request: dict[str, Any] = dict(
                    model=self.model,
                    image=source,
                    prompt=prompt,
                    quality=self.quality,
                    size=self.size,
                    output_format=self.output_format,
                )
                # GPT Image 2 always uses high input fidelity and rejects attempts
                # to override it. Older compatible models may accept low/high.
                if not self.model.startswith("gpt-image-2") and self.input_fidelity != "auto":
                    request["input_fidelity"] = self.input_fidelity
                raw_api = getattr(self.client.images, "with_raw_response", None)
                if raw_api is not None:
                    raw_response = raw_api.edit(**request)
                    response = raw_response.parse()
                    retries = int(getattr(raw_response, "retries_taken", 0) or 0)
                    http_status = getattr(raw_response, "status_code", None)
                else:
                    response = self.client.images.edit(**request)
                    retries = 0
        except APITimeoutError as exc:
            raise ProviderTimeoutError("Image provider timed out") from exc
        except APIConnectionError as exc:
            raise ProviderUnavailableError("Image provider network failure") from exc
        except RateLimitError as exc:
            if _provider_error_code(exc) == "insufficient_quota":
                raise ProviderQuotaError("Image provider quota is unavailable") from exc
            raise ProviderUnavailableError("Image provider rate limit") from exc
        except BadRequestError as exc:
            if _provider_error_code(exc) in {
                "moderation_blocked", "content_policy_violation",
            }:
                raise PolicyRejectedError("The image request was rejected by provider policy") from exc
            raise ProviderUnavailableError("Image provider rejected the request") from exc
        except OpenAIError as exc:
            raise ProviderUnavailableError("Image provider request failed") from exc
        encoded = response.data[0].b64_json
        if not encoded:
            raise RuntimeError("Provider returned no image bytes")
        usage_object = getattr(response, "usage", None)
        if hasattr(usage_object, "model_dump"):
            usage = usage_object.model_dump(mode="json")
        elif isinstance(usage_object, dict):
            usage = usage_object
        else:
            usage = {}
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ProviderUnavailableError(
                "Image provider returned invalid image bytes"
            ) from exc
        return ProviderResult(
            image_bytes=image_bytes,
            request_id=getattr(response, "_request_id", None),
            usage=usage,
            retries=retries,
            provider_name=self.name,
            provider_model=self.model,
            image_model=self.model,
            provider_mode="stateless",
            http_status=http_status,
            provider_duration_ms=int((time.monotonic() - started) * 1000),
        )


class OpenAIResponsesImageProvider:
    """Optional Responses image-tool adapter used only for iterative corrections."""

    name = "openai-responses"

    def __init__(
        self,
        client: Any,
        model: str,
        image_model: str,
        quality: str = "medium",
        size: str = "1024x1024",
        output_format: str = "png",
    ) -> None:
        self.client = client
        self.model = model
        self.image_model = image_model
        self.quality = quality
        self.size = size
        self.output_format = output_format

    @staticmethod
    def _image_data_url(source_path: Path) -> str:
        mime_type = mimetypes.guess_type(source_path.name)[0] or "image/png"
        encoded = base64.b64encode(source_path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _field(item: Any, name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

    def edit_with_context(
        self,
        source_path: Path,
        prompt: str,
        context: ProviderContextRequest,
    ) -> ProviderResult:
        _validate_provider_prompt(prompt)
        started = time.monotonic()
        request: dict[str, Any] = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": self._image_data_url(source_path),
                            "detail": "high",
                        },
                    ],
                }
            ],
            "tools": [
                {
                    "type": "image_generation",
                    "action": "edit",
                    "model": self.image_model,
                    "quality": self.quality,
                    "size": self.size,
                    "output_format": self.output_format,
                }
            ],
            "tool_choice": {"type": "image_generation"},
            # A response chain cannot work with store=false. Retention is guarded
            # before this adapter is called and cleanup deletes stored responses.
            "store": True,
        }
        if context.previous_response_id:
            request["previous_response_id"] = context.previous_response_id
        elif context.conversation_id:
            request["conversation"] = context.conversation_id
        http_status = None
        retries = 0
        raw_response = None
        try:
            raw_api = getattr(self.client.responses, "with_raw_response", None)
            if raw_api is not None:
                raw_response = raw_api.create(**request)
                response = raw_response.parse()
                http_status = getattr(raw_response, "status_code", None)
                retries = int(getattr(raw_response, "retries_taken", 0) or 0)
            else:
                response = self.client.responses.create(**request)
        except APITimeoutError as exc:
            raise ProviderTimeoutError("Responses image provider timed out") from exc
        except APIConnectionError as exc:
            raise ProviderUnavailableError("Responses image provider network failure") from exc
        except RateLimitError as exc:
            if _provider_error_code(exc) == "insufficient_quota":
                raise ProviderQuotaError("Responses image provider quota is unavailable") from exc
            raise ProviderUnavailableError("Responses image provider rate limit") from exc
        except BadRequestError as exc:
            if _provider_error_code(exc) in {
                "moderation_blocked", "content_policy_violation",
            }:
                raise PolicyRejectedError("The image request was rejected by provider policy") from exc
            raise ProviderUnavailableError("Responses image provider rejected the context") from exc
        except OpenAIError as exc:
            raise ProviderUnavailableError("Responses image provider request failed") from exc

        encoded = None
        for item in getattr(response, "output", ()) or ():
            if self._field(item, "type") == "image_generation_call":
                encoded = self._field(item, "result")
                if encoded:
                    break
        if not encoded:
            raise ProviderUnavailableError("Responses image provider returned no image bytes")
        usage_object = getattr(response, "usage", None)
        if hasattr(usage_object, "model_dump"):
            usage = usage_object.model_dump(mode="json")
        elif isinstance(usage_object, dict):
            usage = usage_object
        else:
            usage = {}
        request_id = getattr(response, "_request_id", None)
        if request_id is None and raw_response is not None:
            headers = getattr(raw_response, "headers", {})
            request_id = headers.get("x-request-id") if headers else None
        response_id = getattr(response, "id", None)
        if not response_id:
            raise ProviderUnavailableError("Responses image provider returned no response id")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ProviderUnavailableError(
                "Responses image provider returned invalid image bytes"
            ) from exc
        return ProviderResult(
            image_bytes=image_bytes,
            request_id=request_id,
            usage=usage,
            retries=retries,
            provider_name=self.name,
            provider_model=self.model,
            image_model=self.image_model,
            provider_response_id=response_id,
            provider_conversation_id=context.conversation_id,
            provider_mode="responses",
            context_depth=context.depth,
            http_status=http_status,
            provider_duration_ms=int((time.monotonic() - started) * 1000),
        )


class ContextAwareImageProvider:
    """Keeps the proven Image API as the fail-open production baseline."""

    def __init__(
        self,
        stateless: ImageProvider,
        contextual: OpenAIResponsesImageProvider,
    ) -> None:
        self.stateless = stateless
        self.contextual = contextual
        self.name = stateless.name
        self.model = stateless.model
        self.quality = getattr(stateless, "quality", None)
        self.size = getattr(stateless, "size", None)
        self.output_format = getattr(stateless, "output_format", None)

    def edit(self, source_path: Path, prompt: str) -> ProviderResult:
        return self.stateless.edit(source_path, prompt)

    def edit_with_context(
        self,
        source_path: Path,
        prompt: str,
        context: ProviderContextRequest,
    ) -> ProviderResult:
        try:
            return self.contextual.edit_with_context(source_path, prompt, context)
        except (ProviderTimeoutError, ProviderUnavailableError) as exc:
            fallback = self.stateless.edit(source_path, prompt)
            return replace(
                fallback,
                context_fallback_used=True,
                context_fallback_reason=type(exc).__name__,
            )


class FakeImageProvider:
    """Deterministic local provider for unit and integration tests."""

    name = "fake"
    model = "fake-image-edit-v1"
    quality = "fake"
    size = "source"
    output_format = "png"

    def __init__(self, fail: Exception | None = None, color: str = "#d7e8ff") -> None:
        self.fail = fail
        self.color = color
        self.calls = 0

    def edit(self, source_path: Path, prompt: str) -> ProviderResult:
        _validate_provider_prompt(prompt)
        self.calls += 1
        if self.fail:
            raise self.fail
        with Image.open(source_path) as opened:
            image = opened.convert("RGB")
        overlay = Image.new("RGBA", image.size, self.color)
        overlay.putalpha(45)
        result = Image.alpha_composite(image.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(result)
        draw.rectangle((0, 0, min(40, image.width), min(40, image.height)), fill=self.color)
        buffer = io.BytesIO()
        result.convert("RGB").save(buffer, format="PNG")
        return ProviderResult(
            image_bytes=buffer.getvalue(),
            request_id=f"fake-{self.calls}",
            usage={"fake": True},
            estimated_cost_rub=0.0,
            provider_name=self.name,
            provider_model=self.model,
            image_model=self.model,
        )
