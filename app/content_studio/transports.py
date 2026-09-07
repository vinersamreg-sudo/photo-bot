"""Credential-isolated transports for approved Content Studio publications."""

from __future__ import annotations

import hashlib
import re
import ssl
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from .config import ContentStudioSettings
from .publisher import PlatformPublisher, PublisherAdapter


class ContentPublishingError(RuntimeError):
    """A privacy-safe publishing failure; remote response bodies are never exposed."""


_SECRET_TEXT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bsk-[A-Za-z0-9_-]{20,}\b|"
    r"\bAIza[A-Za-z0-9_-]{30,}\b|"
    r"(?i:\b(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S{12,})"
)


class _RetrySafeTransport:
    def retry(self, payload: dict[str, Any], previous_external_id: str | None) -> str:
        if previous_external_id:
            return previous_external_id
        return self.publish(payload)


class MaxContentClientProtocol(Protocol):
    def audit_permissions(self, channel_id: str) -> dict[str, object]: ...

    def publish_media(self, channel_id: str, media: Path, text: str, source: str) -> str: ...

    def metrics(self, channel_id: str, external_id: str) -> dict[str, int]: ...


class MaxContentApi:
    _UPLOAD_HOST_SUFFIXES = (".oneme.ru", ".okcdn.ru", ".max.ru")

    def __init__(
        self,
        token: str,
        base_url: str,
        *,
        ca_bundle: Path | None = None,
        api_client: httpx.Client | None = None,
        upload_client: httpx.Client | None = None,
        sleeper=time.sleep,
    ) -> None:
        if not token:
            raise ContentPublishingError("MAX Content Studio token is not configured")
        verify: ssl.SSLContext | bool = True
        if ca_bundle is not None:
            if not ca_bundle.is_file():
                raise ContentPublishingError("MAX Content Studio CA bundle is unavailable")
            try:
                verify = ssl.create_default_context(cafile=str(ca_bundle))
            except (OSError, ssl.SSLError) as error:
                raise ContentPublishingError("MAX Content Studio CA bundle is invalid") from error
        self.api_client = api_client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": token, "User-Agent": "ravuna-growth-engine/1"},
            timeout=30,
            verify=verify,
        )
        self.upload_client = upload_client or httpx.Client(timeout=90)
        self.sleeper = sleeper

    def audit_permissions(self, channel_id: str) -> dict[str, object]:
        me = self._request("GET", "/me")
        admins = self._request("GET", f"/chats/{channel_id}/members/admins")
        bot_id = str(me.get("user_id") or "")
        match = None
        for member in admins.get("members", []) if isinstance(admins, dict) else []:
            if isinstance(member, dict) and str(member.get("user_id") or "") == bot_id:
                match = member
                break
        permissions = sorted(match.get("permissions") or []) if isinstance(match, dict) else []
        required = {"read_all_messages", "write"}
        return {
            "channel_id": channel_id,
            "bot_membership": match is not None,
            "admin": match is not None,
            "permissions": permissions,
            "read_all_messages": "read_all_messages" in permissions,
            "write": "write" in permissions,
            "ready": match is not None and required.issubset(permissions),
        }

    def publish_media(self, channel_id: str, media: Path, text: str, source: str) -> str:
        permissions = self.audit_permissions(channel_id)
        if permissions["ready"] is not True:
            raise ContentPublishingError("MAX channel permissions are insufficient")
        existing = self._find_existing(channel_id, source)
        if existing:
            return existing
        media_type = "video" if media.suffix.lower() == ".mp4" else "image"
        upload = self._request("POST", "/uploads", params={"type": media_type})
        upload_url = upload.get("url")
        self._validate_upload_url(upload_url)
        try:
            with media.open("rb") as stream:
                response = self.upload_client.post(
                    str(upload_url), files={"data": (media.name, stream, _media_type(media))}
                )
        except httpx.TimeoutException as error:
            raise ContentPublishingError("MAX media upload timed out") from error
        except (OSError, httpx.HTTPError) as error:
            raise ContentPublishingError("MAX media upload failed") from error
        if response.status_code >= 400:
            raise ContentPublishingError(f"MAX media upload returned HTTP {response.status_code}")
        try:
            uploaded = response.json()
        except ValueError as error:
            raise ContentPublishingError("MAX media upload returned invalid JSON") from error
        token = (uploaded.get("token") if isinstance(uploaded, dict) else None) or upload.get("token")
        if not token and isinstance(uploaded, dict):
            photos = uploaded.get("photos")
            if isinstance(photos, dict):
                for photo in photos.values():
                    if isinstance(photo, dict) and photo.get("token"):
                        token = photo["token"]
                        break
        if not isinstance(token, str) or not token:
            raise ContentPublishingError("MAX media upload did not return a token")
        body = {
            "text": text,
            "notify": True,
            "attachments": [{"type": media_type, "payload": {"token": token}}],
        }
        for attempt, delay in enumerate((0, 2, 4, 8)):
            if delay:
                self.sleeper(delay)
            try:
                value = self._request(
                    "POST", "/messages", params={"chat_id": channel_id}, json=body
                )
            except ContentPublishingError as error:
                if "attachment.not.ready" in str(error) and attempt < 3:
                    continue
                raise
            message = value.get("message") if isinstance(value, dict) else None
            body_value = message.get("body") if isinstance(message, dict) else None
            message_id = body_value.get("mid") if isinstance(body_value, dict) else None
            if not message_id:
                raise ContentPublishingError("MAX did not confirm the channel post")
            return str(message_id)
        raise ContentPublishingError("MAX media was not ready after bounded retries")

    def metrics(self, channel_id: str, external_id: str) -> dict[str, int]:
        value = self._request("GET", "/messages", params={"message_ids": external_id})
        messages = value.get("messages", []) if isinstance(value, dict) else []
        message = messages[0] if messages and isinstance(messages[0], dict) else {}
        stat = message.get("stat") if isinstance(message, dict) else {}
        return {
            "views": int((stat or {}).get("views") or 0),
            "reactions": 0,
            "comments": 0,
        }

    def _find_existing(self, channel_id: str, source: str) -> str | None:
        if not source:
            return None
        value = self._request(
            "GET", "/messages", params={"chat_id": channel_id, "count": 100}
        )
        for message in value.get("messages", []) if isinstance(value, dict) else []:
            body = message.get("body") if isinstance(message, dict) else None
            if not isinstance(body, dict) or source not in str(body.get("text") or ""):
                continue
            if body.get("mid"):
                return str(body["mid"])
        return None

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.api_client.request(method, path, **kwargs)
        except httpx.TimeoutException as error:
            raise ContentPublishingError("MAX API request timed out") from error
        except httpx.HTTPError as error:
            raise ContentPublishingError("MAX API request failed") from error
        try:
            value = response.json()
        except ValueError as error:
            raise ContentPublishingError("MAX API returned invalid JSON") from error
        if response.status_code >= 400:
            code = value.get("code") if isinstance(value, dict) else None
            if code == "attachment.not.ready":
                raise ContentPublishingError("attachment.not.ready")
            raise ContentPublishingError(f"MAX API returned HTTP {response.status_code}")
        if not isinstance(value, dict):
            raise ContentPublishingError("MAX API response is invalid")
        return value

    @classmethod
    def _validate_upload_url(cls, value: object) -> None:
        if not isinstance(value, str):
            raise ContentPublishingError("MAX did not return a media upload URL")
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            host == suffix[1:] or host.endswith(suffix) for suffix in cls._UPLOAD_HOST_SUFFIXES
        ):
            raise ContentPublishingError("MAX returned an untrusted media upload URL")


class MaxChannelTransport(_RetrySafeTransport):
    def __init__(self, client: MaxContentClientProtocol, channel_id: str, media_root: Path) -> None:
        if not channel_id:
            raise ContentPublishingError("MAX Content Studio channel is not configured")
        self.client = client
        self.channel_id = channel_id
        self.media_root = media_root.resolve()

    def audit_permissions(self) -> dict[str, object]:
        return self.client.audit_permissions(self.channel_id)

    def publish(self, payload: dict[str, Any]) -> str:
        text, media = _validated_payload(
            payload, platform="max", max_text=4000, media_root=self.media_root
        )
        return self.client.publish_media(
            self.channel_id, media, text, str(payload.get("source_code") or "")
        )

    def metrics(self, external_id: str) -> dict[str, int]:
        return self.client.metrics(self.channel_id, external_id)


class TelegramChannelTransport(_RetrySafeTransport):
    def __init__(
        self,
        token: str,
        channel_id: str,
        media_root: Path,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if not token or not channel_id:
            raise ContentPublishingError("Telegram Content Studio is not configured")
        self.channel_id = channel_id
        self.media_root = media_root.resolve()
        self.client = client or httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}/",
            timeout=30,
            headers={"User-Agent": "ravuna-growth-engine/1"},
        )

    def publish(self, payload: dict[str, Any]) -> str:
        text, media = _validated_payload(
            payload, platform="telegram", max_text=1024, media_root=self.media_root
        )
        method = "sendVideo" if media.suffix.lower() == ".mp4" else "sendPhoto"
        field = "video" if method == "sendVideo" else "photo"
        try:
            with media.open("rb") as stream:
                response = self.client.post(
                    method,
                    data={"chat_id": self.channel_id, "caption": text},
                    files={field: (media.name, stream, _media_type(media))},
                )
        except httpx.TimeoutException as error:
            raise ContentPublishingError("Telegram publication timed out") from error
        except (OSError, httpx.HTTPError) as error:
            raise ContentPublishingError("Telegram publication failed") from error
        if response.status_code >= 400:
            raise ContentPublishingError(f"Telegram publication returned HTTP {response.status_code}")
        try:
            value = response.json()
        except ValueError as error:
            raise ContentPublishingError("Telegram returned invalid JSON") from error
        result = value.get("result") if isinstance(value, dict) and value.get("ok") is True else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            raise ContentPublishingError("Telegram did not confirm the channel post")
        return str(message_id)


class VkCommunityTransport(_RetrySafeTransport):
    _UPLOAD_HOST_SUFFIXES = (".vk.com", ".vk.ru", ".userapi.com", ".vkvideo.ru")

    def __init__(
        self,
        access_token: str,
        group_id: str,
        media_root: Path,
        *,
        token_type: str = "user",
        api_version: str = "5.199",
        api_client: httpx.Client | None = None,
        upload_client: httpx.Client | None = None,
    ) -> None:
        if not access_token or not group_id.isdigit() or int(group_id) <= 0:
            raise ContentPublishingError("VK Content Studio is not configured")
        if token_type != "user":
            raise ContentPublishingError(
                "VK wall and video publication requires a user token in the official API schema"
            )
        self.access_token = access_token
        self.group_id = group_id
        self.api_version = api_version
        self.media_root = media_root.resolve()
        self.api_client = api_client or httpx.Client(
            base_url="https://api.vk.com/method/",
            timeout=30,
            headers={"User-Agent": "ravuna-growth-engine/1"},
        )
        self.upload_client = upload_client or httpx.Client(timeout=120)

    def audit_permissions(self) -> dict[str, object]:
        value = self._call(
            "groups.getById",
            {
                "group_ids": self.group_id,
                "fields": "can_post,can_upload_video,can_upload_clip",
            },
        )
        groups = value.get("groups", []) if isinstance(value, dict) else value
        group = groups[0] if isinstance(groups, list) and groups else {}
        can_post = bool(group.get("can_post")) if isinstance(group, dict) else False
        can_video = bool(group.get("can_upload_video")) if isinstance(group, dict) else False
        return {
            "group_id": self.group_id,
            "authenticated": True,
            "can_post": can_post,
            "can_upload_video": can_video,
            "ready": can_post and can_video,
        }

    def publish(self, payload: dict[str, Any]) -> str:
        text, media = _validated_payload(
            payload, platform="vk", max_text=4096, media_root=self.media_root
        )
        permissions = self.audit_permissions()
        if permissions["ready"] is not True:
            raise ContentPublishingError("VK community permissions are insufficient")
        source = str(payload.get("source_code") or "")
        existing = self._find_existing(source)
        if existing:
            return existing
        if media.suffix.lower() == ".mp4":
            return self._publish_video(payload, text, media)
        return self._publish_photo(payload, text, media)

    def metrics(self, external_id: str) -> dict[str, int]:
        post_id = _external_part(external_id, "wall") or external_id
        value = self._call("wall.getById", {"posts": f"-{self.group_id}_{post_id}"})
        items = value.get("items", []) if isinstance(value, dict) else value
        post = items[0] if isinstance(items, list) and items else {}
        views = (post.get("views") or {}).get("count", 0) if isinstance(post, dict) else 0
        likes = (post.get("likes") or {}).get("count", 0) if isinstance(post, dict) else 0
        comments = (post.get("comments") or {}).get("count", 0) if isinstance(post, dict) else 0
        return {
            "views": int(views or 0),
            "reactions": int(likes or 0),
            "comments": int(comments or 0),
        }

    def _publish_photo(self, payload: dict[str, Any], text: str, media: Path) -> str:
        upload = self._call("photos.getWallUploadServer", {"group_id": self.group_id})
        upload_url = upload.get("upload_url") if isinstance(upload, dict) else None
        self._validate_upload_url(upload_url)
        uploaded = self._upload(upload_url, "photo", media)
        saved = self._call(
            "photos.saveWallPhoto",
            {
                "group_id": self.group_id,
                "server": uploaded.get("server", ""),
                "photo": uploaded.get("photo", ""),
                "hash": uploaded.get("hash", ""),
            },
        )
        photo = saved[0] if isinstance(saved, list) and saved else None
        if not isinstance(photo, dict) or "owner_id" not in photo or "id" not in photo:
            raise ContentPublishingError("VK did not confirm the saved wall photo")
        post_id = self._wall_post(payload, text, f"photo{photo['owner_id']}_{photo['id']}")
        return f"wall:{post_id}"

    def _publish_video(self, payload: dict[str, Any], text: str, media: Path) -> str:
        saved = self._call(
            "video.save",
            {
                "group_id": self.group_id,
                "name": str(payload.get("post_id") or "Ravuna")[:120],
                "description": text,
                "wallpost": "0",
                "is_private": "0",
            },
        )
        upload_url = saved.get("upload_url") if isinstance(saved, dict) else None
        self._validate_upload_url(upload_url)
        uploaded = self._upload(upload_url, "video_file", media)
        owner_id = uploaded.get("owner_id") or saved.get("owner_id")
        video_id = uploaded.get("video_id") or saved.get("video_id")
        if owner_id is None or video_id is None:
            raise ContentPublishingError("VK did not confirm the uploaded video")
        post_id = self._wall_post(payload, text, f"video{owner_id}_{video_id}")
        return f"wall:{post_id};video:{video_id}"

    def _wall_post(self, payload: dict[str, Any], text: str, attachment: str) -> str:
        posted = self._call(
            "wall.post",
            {
                "owner_id": f"-{self.group_id}",
                "from_group": "1",
                "message": text,
                "attachments": attachment,
                "guid": hashlib.sha256(str(payload["post_id"]).encode("utf-8")).hexdigest()[:32],
            },
        )
        post_id = posted.get("post_id") if isinstance(posted, dict) else None
        if post_id is None:
            raise ContentPublishingError("VK did not confirm the community post")
        return str(post_id)

    def _find_existing(self, source: str) -> str | None:
        if not source:
            return None
        value = self._call("wall.get", {"owner_id": f"-{self.group_id}", "count": "100"})
        items = value.get("items", []) if isinstance(value, dict) else []
        for post in items:
            if isinstance(post, dict) and source in str(post.get("text") or "") and post.get("id"):
                return f"wall:{post['id']}"
        return None

    def _upload(self, upload_url: object, field: str, media: Path) -> dict[str, Any]:
        try:
            with media.open("rb") as stream:
                response = self.upload_client.post(
                    str(upload_url), files={field: (media.name, stream, _media_type(media))}
                )
        except httpx.TimeoutException as error:
            raise ContentPublishingError("VK media upload timed out") from error
        except (OSError, httpx.HTTPError) as error:
            raise ContentPublishingError("VK media upload failed") from error
        if response.status_code >= 400:
            raise ContentPublishingError(f"VK media upload returned HTTP {response.status_code}")
        try:
            value = response.json()
        except ValueError as error:
            raise ContentPublishingError("VK media upload returned invalid JSON") from error
        if not isinstance(value, dict):
            raise ContentPublishingError("VK media upload response is invalid")
        return value

    def _call(self, method: str, data: dict[str, Any]) -> Any:
        fields = {**data, "access_token": self.access_token, "v": self.api_version}
        try:
            response = self.api_client.post(method, data=fields)
        except httpx.TimeoutException as error:
            raise ContentPublishingError("VK API request timed out") from error
        except httpx.HTTPError as error:
            raise ContentPublishingError("VK API request failed") from error
        if response.status_code >= 400:
            raise ContentPublishingError(f"VK API returned HTTP {response.status_code}")
        try:
            value = response.json()
        except ValueError as error:
            raise ContentPublishingError("VK API returned invalid JSON") from error
        if not isinstance(value, dict) or "response" not in value:
            raise ContentPublishingError("VK API rejected the request")
        return value["response"]

    @classmethod
    def _validate_upload_url(cls, value: object) -> None:
        if not isinstance(value, str):
            raise ContentPublishingError("VK did not return a media upload URL")
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            host == suffix[1:] or host.endswith(suffix) for suffix in cls._UPLOAD_HOST_SUFFIXES
        ):
            raise ContentPublishingError("VK returned an untrusted media upload URL")


def build_publishers(settings: ContentStudioSettings) -> dict[str, PublisherAdapter]:
    """Build fail-closed adapters without sharing credentials between platforms."""

    publishers: dict[str, PublisherAdapter] = {}
    max_transport = None
    if settings.max_publishing_enabled:
        project_candidate = (
            Path(__file__).resolve().parents[2]
            / "ops"
            / "certs"
            / "russian_trusted_root_ca_pem.crt"
        )
        max_transport = MaxChannelTransport(
            MaxContentApi(
                settings.max_bot_token,
                settings.max_api_base_url,
                ca_bundle=project_candidate if project_candidate.is_file() else None,
            ),
            settings.max_channel_id,
            settings.storage_dir,
        )
    publishers["max"] = PlatformPublisher(
        platform="max",
        publishing_enabled=settings.publishing_enabled and settings.max_publishing_enabled,
        transport=max_transport,
    )

    telegram_transport = None
    if settings.telegram_publishing_enabled:
        telegram_transport = TelegramChannelTransport(
            settings.telegram_bot_token,
            settings.telegram_channel_id,
            settings.storage_dir,
        )
    publishers["telegram"] = PlatformPublisher(
        platform="telegram",
        publishing_enabled=settings.publishing_enabled and settings.telegram_publishing_enabled,
        transport=telegram_transport,
    )

    vk_transport = None
    if settings.vk_publishing_enabled:
        vk_transport = VkCommunityTransport(
            settings.vk_access_token,
            settings.vk_group_id,
            settings.storage_dir,
            token_type=settings.vk_token_type,
            api_version=settings.vk_api_version,
        )
    publishers["vk"] = PlatformPublisher(
        platform="vk",
        publishing_enabled=settings.publishing_enabled and settings.vk_publishing_enabled,
        transport=vk_transport,
    )
    return publishers


def _validated_payload(
    payload: dict[str, Any], *, platform: str, max_text: int, media_root: Path
) -> tuple[str, Path]:
    if payload.get("platform") != platform:
        raise ContentPublishingError("Content Studio platform payload mismatch")
    if not str(payload.get("source_code") or "").startswith("src_"):
        raise ContentPublishingError("Content Studio attribution source is missing")
    text = str(payload.get("text") or "").strip()
    if not text or len(text) > max_text:
        raise ContentPublishingError(f"{platform.upper()} post text is outside safe limits")
    if _SECRET_TEXT.search(text):
        raise ContentPublishingError("Content Studio post text failed the secret gate")
    media = Path(str(payload.get("media_path") or "")).resolve()
    if not media.is_absolute() or not media.is_file():
        raise ContentPublishingError("Content Studio media is unavailable")
    try:
        media.relative_to(media_root)
    except ValueError as error:
        raise ContentPublishingError("Content Studio media is outside isolated storage") from error
    if media.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".mp4"}:
        raise ContentPublishingError("Content Studio media format is not supported")
    return text, media


def _media_type(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
    }.get(path.suffix.lower(), "application/octet-stream")


def _external_part(value: str, name: str) -> str | None:
    prefix = f"{name}:"
    for part in value.split(";"):
        if part.startswith(prefix):
            return part[len(prefix) :]
    return None
