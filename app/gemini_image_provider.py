"""Google Gemini Interactions adapter for native image editing."""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx
from PIL import Image, ImageOps

from app.domain import (
    PolicyRejectedError,
    ProviderInvalidRequestError,
    ProviderQuotaError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com"
_ASPECT_RATIOS = (
    "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"
)
_POLICY_MARKERS = (
    "policy", "safety", "blocked", "prohibited", "not supported", "refus",
    "политик", "безопасност", "отклон", "не поддерж",
)


class GeminiConfigurationError(RuntimeError):
    """Raised when the selected Gemini route has incomplete configuration."""


def create_gemini_client(api_key: str, timeout_seconds: int) -> httpx.Client:
    if not api_key:
        raise GeminiConfigurationError(
            "GEMINI_API_KEY is not configured. Add it to the local .env file."
        )
    return httpx.Client(
        base_url=GEMINI_API_BASE_URL,
        headers={"x-goog-api-key": api_key},
        timeout=timeout_seconds,
    )


def _closest_aspect_ratio(width: int, height: int) -> str:
    target = width / height

    def distance(value: str) -> float:
        left, right = value.split(":", 1)
        return abs(target - (int(left) / int(right)))

    return min(_ASPECT_RATIOS, key=distance)


def _response_format(
    configured_size: str, output_format: str, source_path: Path
) -> dict[str, str] | None:
    if configured_size == "auto":
        return None
    width_text, height_text = configured_size.split("x", 1)
    longest = max(int(width_text), int(height_text))
    image_size = "1K" if longest <= 1536 else "2K" if longest <= 3072 else "4K"
    with Image.open(source_path) as opened:
        width, height = ImageOps.exif_transpose(opened).size
    result = {
        "type": "image",
        "aspect_ratio": _closest_aspect_ratio(width, height),
        "image_size": image_size,
    }
    if output_format in {"png", "jpeg"}:
        result["mime_type"] = f"image/{output_format}"
    return result


def _mapping_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False).casefold()
    except (TypeError, ValueError):
        return str(value).casefold()


def _walk_image_blocks(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        if value.get("type") == "image" and value.get("data"):
            yield str(value["data"])
        for key, child in value.items():
            if key not in {"input", "request"}:
                yield from _walk_image_blocks(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_image_blocks(child)


def _walk_text_blocks(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        if value.get("type") == "text" and value.get("text"):
            yield str(value["text"])
        for key, child in value.items():
            if key not in {"input", "request"}:
                yield from _walk_text_blocks(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_text_blocks(child)


def _output_image_data(payload: Mapping[str, Any]) -> str | None:
    direct = payload.get("output_image")
    if isinstance(direct, Mapping) and direct.get("data"):
        return str(direct["data"])
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in reversed(steps):
            if isinstance(step, Mapping) and step.get("type") == "model_output":
                return next(_walk_image_blocks(step.get("content")), None)
    for key in ("output", "outputs"):
        encoded = next(_walk_image_blocks(payload.get(key)), None)
        if encoded:
            return encoded
    return None


def _output_text(payload: Mapping[str, Any]) -> str:
    direct = str(payload.get("output_text") or "").strip()
    if direct:
        return direct
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in reversed(steps):
            if isinstance(step, Mapping) and step.get("type") == "model_output":
                text = next(_walk_text_blocks(step.get("content")), "").strip()
                if text:
                    return text
    return next(_walk_text_blocks(payload.get("output")), "").strip()


class GeminiImageProvider:
    """Stateless image edit via the official Gemini Interactions REST API."""

    quality = "provider-default"

    def __init__(
        self,
        client: Any,
        model: str,
        *,
        provider_name: str = "gemini",
        size: str = "auto",
        output_format: str = "png",
    ) -> None:
        if provider_name not in {"gemini", "nanobanana"}:
            raise ValueError("Gemini provider name must be 'gemini' or 'nanobanana'")
        if not model:
            raise GeminiConfigurationError("Gemini image model is not configured")
        self.client = client
        self.model = model
        self.name = provider_name
        self.size = size
        self.output_format = output_format

    def resolve_size(self, source_path: Path) -> str:
        configured = _response_format(self.size, self.output_format, source_path)
        if configured is None:
            return "auto"
        return f"{configured['image_size']}/{configured['aspect_ratio']}"

    def edit(self, source_path: Path, prompt: str) -> ProviderResult:
        if not prompt.strip():
            raise ValueError("Image providers require a non-empty prompt")
        started = time.monotonic()
        mime_type = mimetypes.guess_type(source_path.name)[0] or "image/png"
        request: dict[str, Any] = {
            "model": self.model,
            "input": [
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "mime_type": mime_type,
                    "data": base64.b64encode(source_path.read_bytes()).decode("ascii"),
                },
            ],
        }
        response_format = _response_format(self.size, self.output_format, source_path)
        if response_format is not None:
            request["response_format"] = response_format
        try:
            response = self.client.post("/v1beta/interactions", json=request)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Image provider timed out") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("Image provider network failure") from exc

        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise ProviderUnavailableError("Image provider returned invalid JSON") from exc
        if response.status_code >= 400:
            details = _mapping_text(payload)
            if response.status_code == 429:
                raise ProviderQuotaError("Image provider quota is unavailable")
            if response.status_code in {400, 403} and any(
                marker in details for marker in _POLICY_MARKERS
            ):
                raise PolicyRejectedError("The image request was rejected by provider policy")
            if response.status_code in {400, 422}:
                raise ProviderInvalidRequestError("Image provider rejected an invalid request")
            raise ProviderUnavailableError(
                f"Image provider request failed with HTTP {response.status_code}"
            )
        if not isinstance(payload, Mapping):
            raise ProviderUnavailableError("Image provider returned an invalid response")
        encoded = _output_image_data(payload)
        if not encoded:
            output_text = _output_text(payload)
            if output_text:
                raise PolicyRejectedError("The image provider returned a text refusal")
            raise ProviderUnavailableError("Image provider returned no image bytes")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ProviderUnavailableError(
                "Image provider returned invalid image bytes"
            ) from exc
        usage = payload.get("usage") or payload.get("usage_metadata") or {}
        if not isinstance(usage, Mapping):
            usage = {}
        return ProviderResult(
            image_bytes=image_bytes,
            request_id=response.headers.get("x-request-id"),
            usage=dict(usage),
            provider_name=self.name,
            provider_model=self.model,
            image_model=self.model,
            provider_response_id=(str(payload["id"]) if payload.get("id") else None),
            provider_mode="stateless",
            http_status=response.status_code,
            provider_duration_ms=int((time.monotonic() - started) * 1000),
        )
