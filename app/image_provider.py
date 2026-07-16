"""Replaceable image-edit provider boundary."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageDraw
from openai import BadRequestError

from app.domain import PolicyRejectedError, ProviderResult


class ImageProvider(Protocol):
    name: str
    model: str

    def edit(self, source_path: Path, prompt: str) -> ProviderResult: ...


def _validate_provider_prompt(prompt: str) -> None:
    if not prompt.strip() or not prompt.isascii():
        raise ValueError("Image providers accept normalized English ASCII prompts only")


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
                else:
                    response = self.client.images.edit(**request)
                    retries = 0
        except BadRequestError as exc:
            if getattr(exc, "code", None) == "moderation_blocked":
                raise PolicyRejectedError("The image request was rejected by provider policy") from exc
            raise
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
        return ProviderResult(
            image_bytes=base64.b64decode(encoded),
            request_id=getattr(response, "_request_id", None),
            usage=usage,
            retries=retries,
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
        )
