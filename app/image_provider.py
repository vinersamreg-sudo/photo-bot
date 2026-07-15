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


class OpenAIImageProvider:
    name = "openai"

    def __init__(self, client: Any, model: str, quality: str = "low") -> None:
        self.client = client
        self.model = model
        self.quality = quality
        self.size = "1024x1024"
        self.output_format = "png"

    def edit(self, source_path: Path, prompt: str) -> ProviderResult:
        try:
            with source_path.open("rb") as source:
                response = self.client.images.edit(
                    model=self.model,
                    image=source,
                    prompt=prompt,
                    quality=self.quality,
                    size=self.size,
                    output_format=self.output_format,
                )
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
            retries=0,
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
