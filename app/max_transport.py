"""Thin client for the documented MAX Bot HTTPS API."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse

import httpx

from app.max_adapter import Button


class MaxTransportError(RuntimeError):
    """Safe transport error which never includes response bodies or secret URLs."""


@dataclass(frozen=True)
class MaxIncomingEvent:
    event_type: str
    event_key: str
    user_id: str
    chat_id: Optional[str]
    timestamp_ms: int
    message_id: Optional[str] = None
    text: Optional[str] = None
    image_url: Optional[str] = None
    callback_id: Optional[str] = None
    callback_payload: Optional[str] = None


def _string_id(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def parse_update(update: dict[str, Any]) -> Optional[MaxIncomingEvent]:
    """Normalize only the documented events used by the application."""

    event_type = str(update.get("update_type") or "")
    timestamp = int(update.get("timestamp") or 0)
    if event_type == "bot_started":
        user = update.get("user") or {}
        user_id = _string_id(user.get("user_id"))
        chat_id = _string_id(update.get("chat_id"))
        if not user_id:
            return None
        return MaxIncomingEvent(
            event_type,
            f"start:{chat_id or '-'}:{user_id}:{timestamp}",
            user_id,
            chat_id,
            timestamp,
        )

    message = update.get("message") or {}
    body = message.get("body") or {}
    recipient = message.get("recipient") or {}
    if event_type == "message_created":
        sender = message.get("sender") or {}
        user_id = _string_id(sender.get("user_id"))
        message_id = _string_id(body.get("mid"))
        if not user_id or not message_id:
            return None
        image_url = None
        for attachment in body.get("attachments") or []:
            if attachment.get("type") == "image":
                image_url = (attachment.get("payload") or {}).get("url")
                if image_url:
                    break
        return MaxIncomingEvent(
            event_type,
            f"message:{message_id}",
            user_id,
            _string_id(recipient.get("chat_id")),
            timestamp,
            message_id=message_id,
            text=body.get("text"),
            image_url=image_url,
        )

    if event_type == "message_callback":
        callback = update.get("callback") or {}
        user = callback.get("user") or {}
        callback_id = _string_id(callback.get("callback_id"))
        user_id = _string_id(user.get("user_id"))
        if not callback_id or not user_id:
            return None
        return MaxIncomingEvent(
            event_type,
            f"callback:{callback_id}",
            user_id,
            _string_id(recipient.get("chat_id")),
            timestamp,
            message_id=_string_id(body.get("mid")),
            callback_id=callback_id,
            callback_payload=callback.get("payload"),
        )
    return None


class MaxApiClient:
    """Small synchronous boundary around the official API contract."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://platform-api2.max.ru",
        *,
        timeout_seconds: float = 30,
        media_host_suffixes: Sequence[str] = (".max.ru", ".oneme.ru", ".okcdn.ru"),
        client: Optional[httpx.Client] = None,
    ) -> None:
        if not token:
            raise MaxTransportError("MAX_BOT_TOKEN is not configured")
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.media_host_suffixes = tuple(suffix.lower() for suffix in media_host_suffixes)
        self.client = client or httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": token, "User-Agent": "photo-bot/1"},
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise MaxTransportError("MAX API request timed out") from exc
        except httpx.HTTPError as exc:
            raise MaxTransportError(
                f"MAX API network failure ({type(exc).__name__})"
            ) from exc
        if response.status_code >= 400:
            raise MaxTransportError(f"MAX API returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise MaxTransportError("MAX API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MaxTransportError("MAX API returned unexpected data")
        return payload

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def get_updates(
        self,
        marker: Optional[int],
        *,
        timeout: int = 30,
        types: Iterable[str] = ("bot_started", "message_created", "message_callback"),
    ) -> tuple[list[dict[str, Any]], Optional[int]]:
        params: list[tuple[str, Any]] = [("timeout", timeout)]
        if marker is not None:
            params.append(("marker", marker))
        for event_type in types:
            params.append(("types", event_type))
        data = self._request("GET", "/updates", params=params)
        updates = data.get("updates") or []
        if not isinstance(updates, list):
            raise MaxTransportError("MAX updates payload is invalid")
        next_marker = data.get("marker")
        return updates, int(next_marker) if next_marker is not None else None

    @staticmethod
    def _keyboard(buttons: Sequence[Button]) -> list[dict[str, Any]]:
        if not buttons:
            return []
        return [{
            "type": "inline_keyboard",
            "payload": {
                "buttons": [[{
                    "type": "callback",
                    "text": button.text,
                    "payload": button.action,
                }] for button in buttons]
            },
        }]

    def send_message(
        self,
        user_id: str,
        text: str,
        buttons: Sequence[Button] = (),
        *,
        image_token: Optional[str] = None,
        notify: bool = True,
    ) -> str:
        attachments: list[dict[str, Any]] = []
        if image_token:
            attachments.append({"type": "image", "payload": {"token": image_token}})
        attachments.extend(self._keyboard(buttons))
        body: dict[str, Any] = {"text": text, "notify": notify}
        if attachments:
            body["attachments"] = attachments
        data = self._request("POST", "/messages", params={"user_id": user_id}, json=body)
        message = data.get("message") if isinstance(data.get("message"), dict) else data
        message_id = ((message.get("body") or {}).get("mid")) if isinstance(message, dict) else None
        if not message_id:
            raise MaxTransportError("MAX did not confirm a message id")
        return str(message_id)

    def edit_message(
        self, message_id: str, text: str, buttons: Sequence[Button] = ()
    ) -> None:
        body: dict[str, Any] = {"text": text}
        if buttons:
            body["attachments"] = self._keyboard(buttons)
        self._request("PUT", "/messages", params={"message_id": message_id}, json=body)

    def answer_callback(self, callback_id: str, notification: str) -> None:
        self._request(
            "POST", "/answers", params={"callback_id": callback_id},
            json={"notification": notification},
        )

    def delete_message(self, message_id: str) -> None:
        self._request("DELETE", "/messages", params={"message_id": message_id})

    def _validate_media_url(self, url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            raise MaxTransportError("MAX media URL is not HTTPS")
        if not any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in self.media_host_suffixes):
            raise MaxTransportError("MAX media URL host is not allowed")

    def download_image(self, url: str, destination: Path, max_bytes: int) -> Path:
        self._validate_media_url(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        total = 0
        try:
            with httpx.stream("GET", url, timeout=self.timeout_seconds) as response:
                if response.status_code >= 400:
                    raise MaxTransportError(f"MAX media download returned HTTP {response.status_code}")
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise MaxTransportError("MAX media exceeds the configured size limit")
                        output.write(chunk)
            if total == 0:
                raise MaxTransportError("MAX media download is empty")
            os.replace(temporary, destination)
        except httpx.HTTPError as exc:
            raise MaxTransportError(
                f"MAX media download failed ({type(exc).__name__})"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def upload_image(self, image: Path) -> str:
        upload = self._request("POST", "/uploads", params={"type": "image"})
        url = upload.get("url")
        if not isinstance(url, str):
            raise MaxTransportError("MAX did not return an image upload URL")
        self._validate_media_url(url)
        try:
            with image.open("rb") as content:
                response = httpx.post(
                    url,
                    files={"data": (image.name, content, "application/octet-stream")},
                    timeout=self.timeout_seconds,
                )
        except httpx.HTTPError as exc:
            raise MaxTransportError(f"MAX upload failed ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise MaxTransportError(f"MAX upload returned HTTP {response.status_code}")
        try:
            result = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise MaxTransportError("MAX upload returned invalid JSON") from exc
        token = result.get("token") or upload.get("token")
        if not token:
            raise MaxTransportError("MAX upload did not return a media token")
        return str(token)

    def send_image(
        self,
        platform_user_id: str,
        image: Path,
        caption: str,
        buttons: Sequence[Button],
    ) -> bool:
        try:
            token = self.upload_image(image)
            self.send_message(platform_user_id, caption, buttons, image_token=token)
            return True
        except MaxTransportError:
            return False


class SingleInstanceLock:
    """Cross-platform advisory lock used to prevent two polling consumers."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any = None

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        self._file.seek(0)
        self._file.write("0")
        self._file.flush()
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            raise MaxTransportError("Another MAX polling process already holds the lock") from exc
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
