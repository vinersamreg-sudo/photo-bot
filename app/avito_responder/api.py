"""Small Avito Messenger client with bounded retries and safe errors."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from .models import ChatMessage


class AvitoApiError(RuntimeError):
    def __init__(self, operation: str, status: int | None, *, uncertain: bool = False) -> None:
        super().__init__(f"Avito API {operation} failed" + (f" ({status})" if status else ""))
        self.operation = operation
        self.status = status
        self.uncertain = uncertain


class AvitoApiClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        *,
        timeout_seconds: int = 10,
        allow_send: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.allow_send = allow_send
        self.client = httpx.Client(timeout=timeout_seconds, transport=transport)
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    def get_self(self) -> dict[str, Any]:
        return self._request("GET", "/core/v1/accounts/self", operation="get_self")

    def get_chat(self, user_id: int, chat_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/messenger/v2/accounts/{user_id}/chats/{chat_id}", operation="get_chat"
        )

    def get_messages(self, user_id: int, chat_id: str, *, limit: int) -> tuple[ChatMessage, ...]:
        result = self._request(
            "GET",
            f"/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
            operation="get_messages",
            params={"limit": limit, "offset": 0},
        )
        values = result.get("messages") or ()
        return tuple(sorted((ChatMessage.from_api(value) for value in values), key=lambda item: item.created))

    def send_message(self, user_id: int, chat_id: str, text: str) -> str:
        if not self.allow_send:
            raise AvitoApiError("send_disabled", None)
        result = self._request(
            "POST",
            f"/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages",
            operation="send_message",
            json={"message": {"text": text}, "type": "text"},
            mutation=True,
        )
        message_id = str(result.get("id") or result.get("message", {}).get("id") or "").strip()
        if not message_id:
            raise AvitoApiError("send_message_invalid_response", 200, uncertain=True)
        return message_id

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            try:
                response = self.client.post(
                    f"{self.base_url}/token/",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
            except httpx.HTTPError as exc:
                raise AvitoApiError("token", None) from exc
            if response.status_code != 200:
                raise AvitoApiError("token", response.status_code)
            payload = response.json()
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise AvitoApiError("token_invalid_response", 200)
            expires = max(60, int(payload.get("expires_in") or 86400))
            self._token = token
            self._token_expires_at = time.monotonic() + expires - 60
            return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        mutation: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._access_token()}"
        try:
            response = self.client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AvitoApiError(operation, None, uncertain=mutation) from exc
        if response.status_code == 401:
            self._token = ""
            self._token_expires_at = 0.0
        if not 200 <= response.status_code < 300:
            raise AvitoApiError(operation, response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AvitoApiError(operation + "_invalid_response", response.status_code, uncertain=mutation) from exc
        return payload if isinstance(payload, dict) else {}
