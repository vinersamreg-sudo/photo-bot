"""Platform adapter boundary for review-first publication.

No network transport is constructed by Content Studio v1. Production remains
fail-closed until both an explicit flag and an injected adapter are present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import PublicationMode


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


class MaxPublisher:
    platform = "max"

    def __init__(
        self,
        *,
        publishing_enabled: bool = False,
        transport: PlatformTransport | None = None,
    ) -> None:
        self.publishing_enabled = publishing_enabled
        self.transport = transport

    def preview(self, post: dict[str, Any], media_path: str) -> PublicationOutcome:
        return PublicationOutcome(
            PublicationMode.PREVIEW,
            "planned",
            self._payload(post, media_path),
        )

    def dry_run(self, post: dict[str, Any], media_path: str) -> PublicationOutcome:
        return PublicationOutcome(
            PublicationMode.DRY_RUN,
            "simulated",
            self._payload(post, media_path),
        )

    def manual_publish(
        self, post: dict[str, Any], media_path: str, external_id: str
    ) -> PublicationOutcome:
        if not external_id.strip():
            raise ValueError("manual_publish requires an external publication id")
        return PublicationOutcome(
            PublicationMode.MANUAL_PUBLISH,
            "succeeded",
            self._payload(post, media_path),
            external_id.strip(),
        )

    def publish(self, post: dict[str, Any], media_path: str) -> PublicationOutcome:
        self._require_transport()
        payload = self._payload(post, media_path)
        external_id = self.transport.publish(payload)  # type: ignore[union-attr]
        return PublicationOutcome(PublicationMode.PUBLISH, "succeeded", payload, external_id)

    def retry(self, post: dict[str, Any], media_path: str) -> PublicationOutcome:
        self._require_transport()
        payload = self._payload(post, media_path)
        external_id = self.transport.retry(  # type: ignore[union-attr]
            payload, post.get("published_external_id")
        )
        return PublicationOutcome(PublicationMode.RETRY, "succeeded", payload, external_id)

    def _require_transport(self) -> None:
        if not self.publishing_enabled:
            raise PublishingDisabledError("Content Studio publishing is disabled")
        if self.transport is None:
            raise PublishingDisabledError("MAX publisher transport is not configured")

    @staticmethod
    def _payload(post: dict[str, Any], media_path: str) -> dict[str, Any]:
        return {
            "platform": "max",
            "post_id": post["id"],
            "text": f"{post['title']}\n\n{post['body']}\n\n{post['cta']}\n\n{_hashtags(post)}",
            "media_path": media_path,
            "utm_url": post["utm_url"],
            "demo_disclosure_present": (
                "Демонстрационный пример Pixora." in post["body"]
                and "Изображения созданы специально" in post["body"]
            ),
        }


def _hashtags(post: dict[str, Any]) -> str:
    import json

    values = json.loads(post["hashtags_json"])
    return " ".join(values)
