import base64
import io
import tempfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

import httpx
from PIL import Image
from openai import APIConnectionError, APITimeoutError, BadRequestError, OpenAI, RateLimitError

from app.config import load_settings
from app.domain import (
    PolicyRejectedError,
    ProviderContextRequest,
    ProviderQuotaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.image_provider import ContextAwareImageProvider, OpenAIImageProvider, OpenAIResponsesImageProvider


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


class RawImages(Images):
    def __init__(self, encoded: str) -> None:
        super().__init__(encoded)
        self.with_raw_response = self
        self.retries_taken = 2
        self.parsed = None

    def edit(self, **kwargs):
        self.kwargs = kwargs
        self.parsed = SimpleNamespace(
            data=[SimpleNamespace(b64_json=self.encoded)],
            usage={},
            _request_id="req_raw",
        )
        return self

    def parse(self):
        return self.parsed


class FailingImages:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def edit(self, **_kwargs):
        raise self.error


class OpenAIImageProviderTests(TestCase):
    def test_context_wrapper_preserves_exact_prompt_in_responses_and_stateless_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            Image.new("RGB", (32, 32), "blue").save(source)
            encoded = base64.b64encode(source.read_bytes()).decode("ascii")
            prompt = "  Муж меня обнимает ✨\nЧуть темнее\r\n\t"
            context = ProviderContextRequest("ctx", "resp-parent", None, 2, "branch")
            for fail in (False, True):
                with self.subTest(fallback=fail):
                    requests = []

                    def create(**kwargs):
                        requests.append(kwargs)
                        if fail:
                            raise APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
                        return SimpleNamespace(
                            id="resp-child", _request_id="req-context", usage={},
                            output=[{"type": "image_generation_call", "result": encoded}],
                        )

                    images = Images(encoded)
                    client = SimpleNamespace(images=images, responses=SimpleNamespace(create=create))
                    wrapper = ContextAwareImageProvider(
                        OpenAIImageProvider(client, "gpt-image-2"),
                        OpenAIResponsesImageProvider(client, "gpt-5.4-mini", "gpt-image-2"),
                    )
                    result = wrapper.edit_with_context(source, prompt, context)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0]["input"], [{"role": "user", "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": "data:image/png;base64," + encoded, "detail": "high"},
                    ]}])
                    self.assertEqual(result.image_bytes, source.read_bytes())
                    self.assertEqual(result.context_fallback_used, fail)
                    if fail:
                        self.assertEqual(images.kwargs["prompt"], prompt)
                    else:
                        self.assertIsNone(images.kwargs)

    def test_sdk_multipart_preserves_one_or_two_ordered_original_files_and_prompt(self) -> None:
        prompt = "  На фото 1 добавь мужчину из фото 2 ✨\nБез перевода 👨‍👩‍👧\r\n\t"
        with tempfile.TemporaryDirectory() as directory:
            first, second = (Path(directory) / name for name in ("first.png", "second.png"))
            Image.new("RGB", (48, 32), "blue").save(first)
            Image.new("RGB", (32, 48), "red").save(second)
            output = first.read_bytes()
            captured = []

            def handler(request):
                self.assertEqual(request.url.path, "/v1/images/edits")
                message = BytesParser(policy=policy.default).parsebytes(
                    ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
                    + request.read()
                )
                captured.append([
                    (part.get_param("name", header="content-disposition"),
                     part.get_filename(), part.get_payload(decode=True))
                    for part in message.iter_parts()
                ])
                return httpx.Response(
                    200,
                    headers={"x-request-id": "req-parity"},
                    json={
                        "data": [{"b64_json": base64.b64encode(output).decode("ascii")}],
                        "usage": {"input_tokens": 7, "output_tokens": 11, "total_tokens": 18},
                    },
                )

            with OpenAI(
                api_key="test-only", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            ) as client:
                provider = OpenAIImageProvider(client, "gpt-image-2", quality="medium")
                for sources in ((first,), (first, second), (second, first)):
                    with self.subTest(count=len(sources), first=sources[0].name):
                        result = provider.edit_many(sources, prompt)
                        parts = captured[-1]
                        files = [(name, data) for _, name, data in parts if name]
                        fields = {key: data.decode("utf-8") for key, name, data in parts if not name}
                        self.assertEqual(files, [(path.name, path.read_bytes()) for path in sources])
                        self.assertEqual(fields, {
                            "model": "gpt-image-2", "prompt": prompt, "quality": "medium",
                            "size": "1536x1024" if sources[0] == first else "1024x1536",
                            "output_format": "png",
                        })
                        self.assertEqual(result.image_bytes, output)
                        self.assertEqual((result.request_id, result.http_status), ("req-parity", 200))
                        self.assertEqual(result.usage["total_tokens"], 18)
                        self.assertEqual(result.retries, 0)
                        self.assertEqual(result.provider_mode, "stateless")
                        self.assertGreaterEqual(result.provider_duration_ms, 0)
            self.assertEqual(len(captured), 3)

    def test_edit_many_rejects_invalid_count_before_opening_files_or_calling_api(self) -> None:
        images = Images("")
        provider = OpenAIImageProvider(SimpleNamespace(images=images), "gpt-image-2")
        for paths in ((), (Path("missing.png"),) * 3):
            with self.subTest(count=len(paths)), self.assertRaises(ValueError):
                provider.edit_many(paths, "Запрос")
        self.assertIsNone(images.kwargs)

    def test_uses_images_edit_and_returns_decoded_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = io.BytesIO()
            Image.new("RGB", (64, 64), "white").save(output, format="PNG")
            source.write_bytes(output.getvalue())
            images = Images(base64.b64encode(output.getvalue()).decode("ascii"))
            provider = OpenAIImageProvider(SimpleNamespace(images=images), "gpt-image-2")
            result = provider.edit(source, "Replace the background with a light studio.")
            self.assertEqual(result.image_bytes, output.getvalue())
            self.assertEqual(result.request_id, "req_test")
            self.assertEqual(result.usage["image_tokens"], 196)
            self.assertEqual(images.kwargs["model"], "gpt-image-2")
            self.assertEqual(images.kwargs["quality"], "high")
            self.assertEqual(images.kwargs["size"], "1024x1024")
            self.assertEqual(images.kwargs["output_format"], "png")
            self.assertNotIn("input_fidelity", images.kwargs)

    def test_direct_mode_uses_high_quality_and_unchanged_unicode_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = io.BytesIO()
            Image.new("RGB", (64, 64), "white").save(output, format="PNG")
            source.write_bytes(output.getvalue())
            images = Images(base64.b64encode(output.getvalue()).decode("ascii"))
            settings = load_settings(
                environ={
                    "APP_ENV": "test",
                    "OPENAI_IMAGE_MODEL": "gpt-image-2",
                    "IMAGE_DIRECT_PROMPT_ENABLED": "true",
                }
            )
            provider = OpenAIImageProvider(
                SimpleNamespace(images=images),
                settings.openai_image_model,
                quality=settings.image_edit_quality,
                size=settings.image_edit_size,
                input_fidelity=settings.image_edit_input_fidelity,
                output_format=settings.image_edit_output_format,
            )
            prompt = "сделай меня красивее"

            provider.edit(source, prompt)

            self.assertTrue(settings.image_direct_prompt_enabled)
            self.assertEqual(images.kwargs["prompt"], prompt)
            self.assertEqual(images.kwargs["model"], "gpt-image-2")
            self.assertEqual(images.kwargs["size"], "1024x1024")
            self.assertEqual(images.kwargs["quality"], "high")
            self.assertEqual(images.kwargs["output_format"], "png")

    def test_auto_size_preserves_source_orientation(self) -> None:
        cases = (
            ((442, 960), "1024x1536"),
            ((960, 442), "1536x1024"),
            ((640, 640), "1024x1024"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (dimensions, expected) in enumerate(cases):
                with self.subTest(dimensions=dimensions):
                    source = Path(directory) / f"source-{index}.png"
                    output = io.BytesIO()
                    Image.new("RGB", dimensions, "white").save(output, format="PNG")
                    source.write_bytes(output.getvalue())
                    images = Images(base64.b64encode(output.getvalue()).decode("ascii"))
                    provider = OpenAIImageProvider(
                        SimpleNamespace(images=images), "gpt-image-2", size="auto"
                    )
                    provider.edit(source, "Preserve the composition.")
                    self.assertEqual(images.kwargs["size"], expected)

    def test_explicit_size_remains_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = io.BytesIO()
            Image.new("RGB", (442, 960), "white").save(output, format="PNG")
            source.write_bytes(output.getvalue())
            images = Images(base64.b64encode(output.getvalue()).decode("ascii"))
            provider = OpenAIImageProvider(
                SimpleNamespace(images=images), "gpt-image-2", size="1024x1024"
            )
            provider.edit(source, "Preserve the composition.")
            self.assertEqual(images.kwargs["size"], "1024x1024")

    def test_rejects_empty_provider_prompt_before_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            Image.new("RGB", (32, 32), "white").save(source)
            images = Images("")
            provider = OpenAIImageProvider(SimpleNamespace(images=images), "gpt-image-2")
            with self.assertRaises(ValueError):
                provider.edit(source, "   ")
            self.assertIsNone(images.kwargs)

    def test_input_fidelity_is_omitted_for_image_two_and_configurable_for_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = io.BytesIO()
            Image.new("RGB", (32, 32), "white").save(output, format="PNG")
            source.write_bytes(output.getvalue())
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            current = Images(encoded)
            OpenAIImageProvider(
                SimpleNamespace(images=current), "gpt-image-2", input_fidelity="high"
            ).edit(source, "test")
            self.assertNotIn("input_fidelity", current.kwargs)
            legacy = Images(encoded)
            OpenAIImageProvider(
                SimpleNamespace(images=legacy), "gpt-image-1", input_fidelity="high"
            ).edit(source, "test")
            self.assertEqual(legacy.kwargs["input_fidelity"], "high")

    def test_raw_response_records_actual_sdk_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = io.BytesIO()
            Image.new("RGB", (32, 32), "white").save(output, format="PNG")
            source.write_bytes(output.getvalue())
            images = RawImages(base64.b64encode(output.getvalue()).decode("ascii"))
            result = OpenAIImageProvider(
                SimpleNamespace(images=images), "gpt-image-2"
            ).edit(source, "test")
            self.assertEqual(result.retries, 2)

    def test_provider_errors_are_mapped_to_stable_product_categories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            Image.new("RGB", (32, 32), "white").save(source)
            request = httpx.Request("POST", "https://api.openai.com/v1/images/edits")
            response = httpx.Response(400, request=request)
            cases = (
                (APITimeoutError(request=request), ProviderTimeoutError),
                (APIConnectionError(request=request), ProviderUnavailableError),
                (
                    RateLimitError(
                        "quota", response=httpx.Response(429, request=request),
                        body={"error": {"code": "insufficient_quota"}},
                    ),
                    ProviderQuotaError,
                ),
                (
                    BadRequestError(
                        "blocked", response=response,
                        body={"error": {"code": "content_policy_violation"}},
                    ),
                    PolicyRejectedError,
                ),
            )
            for error, expected in cases:
                for count in (1, 2):
                    with self.subTest(expected=expected.__name__, count=count):
                        provider = OpenAIImageProvider(
                            SimpleNamespace(images=FailingImages(error)), "gpt-image-2"
                        )
                        with self.assertRaises(expected):
                            provider.edit_many((source,) * count, "replace background")
