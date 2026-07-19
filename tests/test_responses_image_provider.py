import base64
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

import httpx
from PIL import Image
from openai import APITimeoutError, BadRequestError, RateLimitError

from app.domain import (
    PolicyRejectedError,
    ProviderContextRequest,
    ProviderQuotaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.image_provider import (
    ContextAwareImageProvider,
    FakeImageProvider,
    OpenAIResponsesImageProvider,
)


class RawResponses:
    def __init__(self, image_bytes: bytes, *, response_id: str | None = "resp-1") -> None:
        self.encoded = base64.b64encode(image_bytes).decode("ascii")
        self.response_id = response_id
        self.kwargs = None
        self.with_raw_response = self
        self.retries_taken = 1
        self.status_code = 200
        self.headers = {"x-request-id": "req-header"}

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self

    def parse(self):
        return SimpleNamespace(
            id=self.response_id,
            output=[SimpleNamespace(type="image_generation_call", result=self.encoded)],
            usage=SimpleNamespace(model_dump=lambda mode: {"total_tokens": 12, "mode": mode}),
            _request_id="req-response",
        )


class MissingImageResponses(RawResponses):
    def parse(self):
        return SimpleNamespace(id="resp-1", output=[], usage={})


class FailingResponses:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def create(self, **_kwargs):
        raise self.error


class ContextualFailure:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def edit_with_context(self, *_args):
        raise self.error


class ResponsesImageProviderTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.source = Path(self.temporary.name) / "source.png"
        output = io.BytesIO()
        Image.new("RGB", (48, 32), "white").save(output, format="PNG")
        self.image_bytes = output.getvalue()
        self.source.write_bytes(self.image_bytes)
        self.context = ProviderContextRequest("ctx", "resp-parent", None, 2, "branch")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _provider(self, responses) -> OpenAIResponsesImageProvider:
        return OpenAIResponsesImageProvider(
            SimpleNamespace(responses=responses),
            "gpt-5.4-mini",
            "gpt-image-2",
            quality="medium",
            size="1024x1024",
            output_format="png",
        )

    def test_01_request_pins_image_model_and_parent_response(self) -> None:
        responses = RawResponses(self.image_bytes)
        self._provider(responses).edit_with_context(self.source, "Preserve identity.", self.context)
        self.assertEqual(responses.kwargs["previous_response_id"], "resp-parent")
        self.assertEqual(responses.kwargs["tools"][0]["model"], "gpt-image-2")
        self.assertEqual(responses.kwargs["model"], "gpt-5.4-mini")

    def test_02_request_includes_previous_original_as_data_url(self) -> None:
        responses = RawResponses(self.image_bytes)
        self._provider(responses).edit_with_context(self.source, "Preserve identity.", self.context)
        image = responses.kwargs["input"][0]["content"][1]
        self.assertTrue(image["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn(str(self.source), str(responses.kwargs))

    def test_03_response_bytes_ids_usage_status_and_depth_are_recorded(self) -> None:
        responses = RawResponses(self.image_bytes)
        result = self._provider(responses).edit_with_context(
            self.source, "Preserve identity.", self.context
        )
        self.assertEqual(result.image_bytes, self.image_bytes)
        self.assertEqual((result.provider_response_id, result.request_id), ("resp-1", "req-response"))
        self.assertEqual((result.http_status, result.context_depth), (200, 2))
        self.assertEqual(result.usage["total_tokens"], 12)

    def test_04_new_chain_omits_previous_response_id(self) -> None:
        responses = RawResponses(self.image_bytes)
        context = ProviderContextRequest("ctx", None, None, 1, "new")
        self._provider(responses).edit_with_context(self.source, "Preserve identity.", context)
        self.assertNotIn("previous_response_id", responses.kwargs)

    def test_05_explicit_conversation_id_is_supported(self) -> None:
        responses = RawResponses(self.image_bytes)
        context = ProviderContextRequest("ctx", None, "conv-1", 1, "new")
        result = self._provider(responses).edit_with_context(
            self.source, "Preserve identity.", context
        )
        self.assertEqual(responses.kwargs["conversation"], "conv-1")
        self.assertEqual(result.provider_conversation_id, "conv-1")

    def test_06_response_chain_is_explicitly_stored_for_later_cleanup(self) -> None:
        responses = RawResponses(self.image_bytes)
        self._provider(responses).edit_with_context(self.source, "Preserve identity.", self.context)
        self.assertIs(responses.kwargs["store"], True)

    def test_07_non_ascii_prompt_is_rejected_before_network(self) -> None:
        responses = RawResponses(self.image_bytes)
        with self.assertRaises(ValueError):
            self._provider(responses).edit_with_context(self.source, "РЎРѕС…СЂР°РЅРё", self.context)
        self.assertIsNone(responses.kwargs)

    def test_08_missing_image_output_is_classified_for_fallback(self) -> None:
        with self.assertRaises(ProviderUnavailableError):
            self._provider(MissingImageResponses(self.image_bytes)).edit_with_context(
                self.source, "Preserve identity.", self.context
            )

    def test_09_missing_response_id_is_classified_for_fallback(self) -> None:
        with self.assertRaises(ProviderUnavailableError):
            self._provider(RawResponses(self.image_bytes, response_id=None)).edit_with_context(
                self.source, "Preserve identity.", self.context
            )

    def test_10_timeout_is_stable_product_error(self) -> None:
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        with self.assertRaises(ProviderTimeoutError):
            self._provider(FailingResponses(APITimeoutError(request=request))).edit_with_context(
                self.source, "Preserve identity.", self.context
            )

    def test_11_policy_error_does_not_become_technical_fallback(self) -> None:
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        error = BadRequestError(
            "blocked",
            response=httpx.Response(400, request=request),
            body={"error": {"code": "content_policy_violation"}},
        )
        with self.assertRaises(PolicyRejectedError):
            self._provider(FailingResponses(error)).edit_with_context(
                self.source, "Preserve identity.", self.context
            )

    def test_12_invalid_previous_response_is_eligible_for_stateless_fallback(self) -> None:
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        error = BadRequestError(
            "invalid previous response",
            response=httpx.Response(400, request=request),
            body={"error": {"code": "invalid_previous_response_id"}},
        )
        with self.assertRaises(ProviderUnavailableError):
            self._provider(FailingResponses(error)).edit_with_context(
                self.source, "Preserve identity.", self.context
            )

    def test_13_quota_error_is_not_retried_as_a_second_paid_call(self) -> None:
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        error = RateLimitError(
            "quota",
            response=httpx.Response(429, request=request),
            body={"error": {"code": "insufficient_quota"}},
        )
        with self.assertRaises(ProviderQuotaError):
            self._provider(FailingResponses(error)).edit_with_context(
                self.source, "Preserve identity.", self.context
            )

    def test_14_context_wrapper_falls_back_once_to_stateless(self) -> None:
        stateless = FakeImageProvider()
        wrapper = ContextAwareImageProvider(
            stateless, ContextualFailure(ProviderUnavailableError("bad context"))
        )
        result = wrapper.edit_with_context(self.source, "Preserve identity.", self.context)
        self.assertEqual(stateless.calls, 1)
        self.assertTrue(result.context_fallback_used)
        self.assertEqual(result.context_fallback_reason, "ProviderUnavailableError")

    def test_15_context_wrapper_does_not_fallback_on_policy(self) -> None:
        stateless = FakeImageProvider()
        wrapper = ContextAwareImageProvider(
            stateless, ContextualFailure(PolicyRejectedError("blocked"))
        )
        with self.assertRaises(PolicyRejectedError):
            wrapper.edit_with_context(self.source, "Preserve identity.", self.context)
        self.assertEqual(stateless.calls, 0)

    def test_16_plain_edit_always_uses_proven_stateless_provider(self) -> None:
        stateless = FakeImageProvider()
        wrapper = ContextAwareImageProvider(
            stateless, ContextualFailure(RuntimeError("must not be called"))
        )
        result = wrapper.edit(self.source, "Preserve identity.")
        self.assertEqual((stateless.calls, result.provider_mode), (1, "stateless"))
