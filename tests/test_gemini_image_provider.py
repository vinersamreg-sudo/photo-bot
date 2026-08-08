import base64
import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase

import httpx
from PIL import Image

from app.domain import (
    PolicyRejectedError,
    ProviderInvalidRequestError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.gemini_image_provider import GeminiImageProvider


class GeminiImageProviderTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / "source.png"
        Image.new("RGB", (1600, 900), "white").save(self.source)
        output = io.BytesIO()
        Image.new("RGB", (32, 32), "blue").save(output, format="PNG")
        self.image_bytes = output.getvalue()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_interactions_payload_keeps_exact_unicode_prompt(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "id": "interaction-1",
                    "output_image": {
                        "type": "image",
                        "data": base64.b64encode(self.image_bytes).decode("ascii"),
                    },
                    "usage": {"total_tokens": 42},
                },
                headers={"x-request-id": "google-request-1"},
            )

        client = httpx.Client(
            base_url="https://generativelanguage.googleapis.com",
            transport=httpx.MockTransport(handler),
        )
        provider = GeminiImageProvider(
            client,
            "gemini-3.1-flash-image",
            size="1024x1024",
            output_format="png",
        )
        user_text = "Изменить размер для загрузки на сотовый телефон"

        result = provider.edit(self.source, user_text)

        self.assertEqual(set(captured), {"model", "input", "response_format"})
        self.assertEqual(captured["model"], "gemini-3.1-flash-image")
        self.assertEqual(captured["input"][0], {"type": "text", "text": user_text})
        self.assertEqual(
            captured["input"][0]["text"].encode("utf-8"),
            user_text.encode("utf-8"),
        )
        self.assertNotIn("Сохрани", captured["input"][0]["text"])
        self.assertNotIn("Измени только", captured["input"][0]["text"])
        self.assertNotIn("preservation", captured["input"][0]["text"].casefold())
        self.assertNotIn("system_instruction", captured)
        self.assertEqual(captured["input"][1]["type"], "image")
        self.assertEqual(captured["input"][1]["mime_type"], "image/png")
        self.assertEqual(
            captured["response_format"],
            {
                "type": "image",
                "aspect_ratio": "16:9",
                "image_size": "1K",
                "mime_type": "image/png",
            },
        )
        self.assertEqual(result.image_bytes, self.image_bytes)
        self.assertEqual(result.provider_name, "gemini")
        self.assertEqual(result.provider_model, "gemini-3.1-flash-image")
        self.assertEqual(result.request_id, "google-request-1")

    def test_two_sources_are_sent_in_order_with_one_unchanged_prompt(self) -> None:
        second = Path(self.temp.name) / "second.png"
        Image.new("RGB", (800, 1200), "red").save(second)
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"output_image": {"data": base64.b64encode(self.image_bytes).decode("ascii")}},
            )

        provider = GeminiImageProvider(
            httpx.Client(
                base_url="https://generativelanguage.googleapis.com",
                transport=httpx.MockTransport(handler),
            ),
            "gemini-3-pro-image",
        )
        prompt = "Муж меня обнимает"

        provider.edit_many((self.source, second), prompt)

        self.assertEqual(captured["input"][0], {"type": "text", "text": prompt})
        self.assertEqual([part["type"] for part in captured["input"]], ["text", "image", "image"])
        self.assertNotIn("Сохрани", captured["input"][0]["text"])
        self.assertNotIn("Измени только", captured["input"][0]["text"])
        self.assertNotIn("system_instruction", captured)

    def test_text_only_provider_refusal_maps_to_policy_error(self) -> None:
        client = httpx.Client(
            base_url="https://generativelanguage.googleapis.com",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "steps": [
                            {
                                "type": "model_output",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "I cannot perform this request.",
                                    }
                                ],
                            }
                        ]
                    },
                )
            ),
        )
        provider = GeminiImageProvider(client, "gemini-3-pro-image")

        with self.assertRaises(PolicyRejectedError):
            provider.edit(self.source, "test")

    def test_policy_http_error_maps_without_exposing_provider_body(self) -> None:
        client = httpx.Client(
            base_url="https://generativelanguage.googleapis.com",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    400,
                    json={"error": {"status": "SAFETY", "message": "blocked by policy"}},
                )
            ),
        )
        provider = GeminiImageProvider(client, "gemini-3-pro-image")

        with self.assertRaisesRegex(PolicyRejectedError, "provider policy"):
            provider.edit(self.source, "test")

    def test_invalid_request_has_a_distinct_error(self) -> None:
        client = httpx.Client(
            base_url="https://generativelanguage.googleapis.com",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    400,
                    json={"error": {"status": "INVALID_ARGUMENT"}},
                )
            ),
        )
        provider = GeminiImageProvider(client, "gemini-3.1-flash-image")

        with self.assertRaises(ProviderInvalidRequestError):
            provider.edit(self.source, "test")

    def test_empty_result_maps_to_provider_unavailable(self) -> None:
        client = httpx.Client(
            base_url="https://generativelanguage.googleapis.com",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"outputs": []})
            ),
        )
        provider = GeminiImageProvider(client, "gemini-3.1-flash-image")

        with self.assertRaisesRegex(ProviderUnavailableError, "no image bytes"):
            provider.edit(self.source, "test")

    def test_timeout_maps_to_provider_timeout(self) -> None:
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        client = httpx.Client(
            base_url="https://generativelanguage.googleapis.com",
            transport=httpx.MockTransport(timeout),
        )
        provider = GeminiImageProvider(client, "gemini-3.1-flash-image")

        with self.assertRaises(ProviderTimeoutError):
            provider.edit(self.source, "test")

    def test_network_failure_maps_to_provider_unavailable(self) -> None:
        def unavailable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unavailable", request=request)

        client = httpx.Client(
            base_url="https://generativelanguage.googleapis.com",
            transport=httpx.MockTransport(unavailable),
        )
        provider = GeminiImageProvider(client, "gemini-3.1-flash-image")

        with self.assertRaises(ProviderUnavailableError):
            provider.edit(self.source, "test")
