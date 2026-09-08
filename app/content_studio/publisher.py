"""Platform adapter boundary for review-first publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from .models import PublicationMode
from .content_generator import validate_customer_copy


class PublishingDisabledError(RuntimeError):
    pass


class PlatformTransport(Protocol):
    def publish(self, payload: dict[str, Any]) -> str: ...

    def retry(self, payload: dict[str, Any], previous_external_id: str | None) -> str: ...


@dataclass(frozen=True)
class PublicationOutcome:
    mode: PublicationMode
    status: str
    payload: dict[str, Any]
    external_id: str | None = None


class PublisherAdapter(Protocol):
    platform: str

    def preview(self, post: dict[str, Any], media_path: str) -> PublicationOutcome: ...

    def dry_run(self, post: dict[str, Any], media_path: str) -> PublicationOutcome: ...

    def manual_publish(
        self, post: dict[str, Any], media_path: str, external_id: str
    ) -> PublicationOutcome: ...

    def publish(self, post: dict[str, Any], media_path: str) -> PublicationOutcome: ...

    def retry(self, post: dict[str, Any], media_path: str) -> PublicationOutcome: ...


class PlatformPublisher:
    def __init__(
        self,
        *,
        platform: str,
        publishing_enabled: bool = False,
        transport: PlatformTransport | None = None,
    ) -> None:
        if platform not in {"max", "telegram", "vk"}:
            raise ValueError("unsupported Content Studio platform")
        self.platform = platform
        self.publishing_enabled = publishing_enabled
        self.transport = transport

    def preview(self, post: dict[str, Any], media_path: str) -> PublicationOutcome:
        payload = self._payload(post, media_path)
        return PublicationOutcome(
            PublicationMode.PREVIEW,
            "planned",
            _audit_payload(payload),
        )

    def dry_run(self, post: dict[str, Any], media_path: str) -> PublicationOutcome:
        payload = self._payload(post, media_path)
        return PublicationOutcome(
            PublicationMode.DRY_RUN,
            "simulated",
            _audit_payload(payload),
        )

    def manual_publish(
        self, post: dict[str, Any], media_path: str, external_id: str
    ) -> PublicationOutcome:
        if not external_id.strip():
            raise ValueError("manual_publish requires an external publication id")
        return PublicationOutcome(
            PublicationMode.MANUAL_PUBLISH,
            "succeeded",
            _audit_payload(self._payload(post, media_path)),
            external_id.strip(),
        )

    def publish(self, post: dict[str, Any], media_path: str) -> PublicationOutcome:
        self._require_transport()
        payload = self._payload(post, media_path)
        external_id = self.transport.publish(payload)  # type: ignore[union-attr]
        return PublicationOutcome(
            PublicationMode.PUBLISH, "succeeded", _audit_payload(payload), external_id
        )

    def retry(self, post: dict[str, Any], media_path: str) -> PublicationOutcome:
        self._require_transport()
        payload = self._payload(post, media_path)
        external_id = self.transport.retry(  # type: ignore[union-attr]
            payload, post.get("published_external_id")
        )
        return PublicationOutcome(
            PublicationMode.RETRY, "succeeded", _audit_payload(payload), external_id
        )

    def _require_transport(self) -> None:
        if not self.publishing_enabled:
            raise PublishingDisabledError("Content Studio publishing is disabled")
        if self.transport is None:
            raise PublishingDisabledError(
                f"{self.platform.upper()} publisher transport is not configured"
            )

    def _payload(self, post: dict[str, Any], media_path: str) -> dict[str, Any]:
        if self.platform == "max":
            validate_customer_copy(str(post["title"]), str(post["body"]))
        disclosure = str(post.get("disclosure") or "")
        return {
            "platform": self.platform,
            "post_id": post["id"],
            "text": f"{post['title']}\n\n{post['body']}\n\n{post['cta']}\n\n{_hashtags(post)}",
            "media_path": media_path,
            "utm_url": post["utm_url"],
            "source_code": post.get("source_code", ""),
            "demo_disclosure_present": bool(disclosure and disclosure in post["body"]),
        }


class MaxPublisher(PlatformPublisher):
    def __init__(
        self,
        *,
        publishing_enabled: bool = False,
        transport: PlatformTransport | None = None,
    ) -> None:
        super().__init__(
            platform="max",
            publishing_enabled=publishing_enabled,
            transport=transport,
        )


def _hashtags(post: dict[str, Any]) -> str:
    values = json.loads(post["hashtags_json"])
    return " ".join(values)


def _audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "media_path": "<content-studio-media>"}
