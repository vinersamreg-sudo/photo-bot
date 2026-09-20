"""Privacy-bounded normalized types for Avito Messenger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class IncomingEvent:
    event_id: str
    message_id: str
    chat_id: str
    author_id: int
    account_user_id: int
    item_id: int
    created_at: datetime
    message_type: str
    direction: str

    @classmethod
    def from_webhook(cls, payload: dict[str, Any]) -> "IncomingEvent":
        envelope_id = str(payload.get("id") or "").strip()
        outer = payload.get("payload") or {}
        value = outer.get("value") or {}
        message_id = str(value.get("id") or "").strip()
        chat_id = str(value.get("chat_id") or "").strip()
        created = int(value.get("created") or payload.get("timestamp") or 0)
        context = value.get("context") or outer.get("context") or {}
        context_value = context.get("value") or {}
        item_id = int(value.get("item_id") or context_value.get("id") or 0)
        if not envelope_id or not message_id or not chat_id or created <= 0:
            raise ValueError("Avito webhook event is missing required identifiers")
        return cls(
            event_id=envelope_id,
            message_id=message_id,
            chat_id=chat_id,
            author_id=int(value.get("author_id") or 0),
            account_user_id=int(value.get("user_id") or 0),
            item_id=item_id,
            created_at=datetime.fromtimestamp(created).astimezone(),
            message_type=str(value.get("type") or "").strip().lower(),
            direction=str(value.get("direction") or "").strip().lower(),
        )


@dataclass(frozen=True)
class ChatMessage:
    message_id: str
    author_id: int
    direction: str
    message_type: str
    text: str
    image_count: int
    created: int

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> "ChatMessage":
        content = value.get("content") or {}
        image = content.get("image")
        images = content.get("images")
        image_count = 0
        if image:
            image_count = 1
        if isinstance(images, list):
            image_count = max(image_count, len(images))
        return cls(
            message_id=str(value.get("id") or ""),
            author_id=int(value.get("author_id") or 0),
            direction=str(value.get("direction") or "").lower(),
            message_type=str(value.get("type") or "").lower(),
            text=str(content.get("text") or "").strip(),
            image_count=image_count,
            created=int(value.get("created") or 0),
        )


@dataclass(frozen=True)
class ReplyContext:
    chat_id: str
    item_id: int
    messages: tuple[ChatMessage, ...]
    image_count: int
    customer_text: str


@dataclass(frozen=True)
class ClaimedChat:
    chat_id: str
    item_id: int
    revision: int
    last_message_id: str
    lock_token: str


@dataclass(frozen=True)
class SendingAttempt:
    chat_id: str
    reply_text: str
    source_revision: int
    current_revision: int
    last_message_id: str
    send_attempts: int
    updated_at: datetime
