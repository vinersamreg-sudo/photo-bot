import base64
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from PIL import Image

from app.image_provider import OpenAIImageProvider


class Images:
    def __init__(self, encoded: str) -> None:
        self.encoded = encoded
        self.kwargs = None

    def edit(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            data=[SimpleNamespace(b64_json=self.encoded)],
            usage=SimpleNamespace(model_dump=lambda mode: {"image_tokens": 196, "mode": mode}),
            _request_id="req_test",
        )


class OpenAIImageProviderTests(TestCase):
    def test_uses_images_edit_and_returns_decoded_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = io.BytesIO()
            Image.new("RGB", (64, 64), "white").save(output, format="PNG")
            source.write_bytes(output.getvalue())
            images = Images(base64.b64encode(output.getvalue()).decode("ascii"))
            provider = OpenAIImageProvider(SimpleNamespace(images=images), "gpt-image-2")
            result = provider.edit(source, "Сделай светлый фон")
            self.assertEqual(result.image_bytes, output.getvalue())
            self.assertEqual(result.request_id, "req_test")
            self.assertEqual(result.usage["image_tokens"], 196)
            self.assertEqual(images.kwargs["model"], "gpt-image-2")
            self.assertEqual(images.kwargs["quality"], "low")
            self.assertEqual(images.kwargs["size"], "1024x1024")
