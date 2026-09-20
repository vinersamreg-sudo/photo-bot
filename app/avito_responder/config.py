"""Fail-closed configuration for the isolated Avito responder."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AvitoResponderSettings:
    base_dir: Path
    database_path: Path
    mode: str = "off"
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    account_user_id: int = 0
    allowed_item_ids: frozenset[int] = frozenset()
    api_base_url: str = "https://api.avito.ru"
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8093
    webhook_secret: str = field(default="", repr=False)
    reply_model: str = "gpt-5.4-mini"
    openai_api_key: str = field(default="", repr=False)
    context_messages: int = 6
    max_context_chars: int = 4000
    reply_timeout_seconds: int = 15
    http_timeout_seconds: int = 10
    debounce_seconds: int = 8
    diagnostic_retention_days: int = 30
    processing_lease_seconds: int = 60
    send_reconcile_grace_seconds: int = 120

    @property
    def webhook_path(self) -> str:
        return f"/integrations/avito/{self.webhook_secret}/messages"

    @property
    def processing_enabled(self) -> bool:
        return self.mode in {"observe", "live"}

    @property
    def auto_reply_enabled(self) -> bool:
        return self.mode == "live"

    @classmethod
    def from_environment(
        cls, project_root: Path, environ: Mapping[str, str] | None = None
    ) -> "AvitoResponderSettings":
        values = os.environ if environ is None else environ
        base_raw = values.get("AVITO_BASE_DIR", "").strip()
        base = Path(base_raw).expanduser() if base_raw else project_root / "data" / "avito_responder"
        if not base.is_absolute():
            base = project_root / base
        base = base.resolve()
        db_raw = values.get("AVITO_DATABASE_PATH", "").strip()
        database_path = Path(db_raw).expanduser() if db_raw else base / "avito_responder.sqlite3"
        if not database_path.is_absolute():
            database_path = project_root / database_path
        api_base_url = values.get("AVITO_API_BASE_URL", "https://api.avito.ru").strip().rstrip("/")
        parsed = urlsplit(api_base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("AVITO_API_BASE_URL must be an absolute HTTPS URL")
        webhook_secret = values.get("AVITO_WEBHOOK_SECRET", "").strip()
        if webhook_secret and not _valid_webhook_secret(webhook_secret):
            raise ValueError("AVITO_WEBHOOK_SECRET must be a 43-character URL-safe secret")
        mode = values.get("AVITO_RESPONDER_MODE", "off").strip().lower() or "off"
        if mode not in {"off", "observe", "live"}:
            raise ValueError("AVITO_RESPONDER_MODE must be off, observe or live")
        settings = cls(
            base_dir=base,
            database_path=database_path.resolve(),
            mode=mode,
            client_id=values.get("AVITO_CLIENT_ID", "").strip(),
            client_secret=values.get("AVITO_CLIENT_SECRET", "").strip(),
            account_user_id=_integer(values, "AVITO_ACCOUNT_USER_ID", 0, minimum=0),
            allowed_item_ids=frozenset(_integer_list(values.get("AVITO_ALLOWED_ITEM_IDS", ""))),
            api_base_url=api_base_url,
            webhook_host=values.get("AVITO_WEBHOOK_HOST", "127.0.0.1").strip(),
            webhook_port=_integer(values, "AVITO_WEBHOOK_PORT", 8093, minimum=1, maximum=65535),
            webhook_secret=webhook_secret,
            reply_model=values.get("AVITO_REPLY_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini",
            openai_api_key=values.get("OPENAI_API_KEY", "").strip(),
            context_messages=_integer(values, "AVITO_REPLY_CONTEXT_MESSAGES", 6, minimum=1, maximum=20),
            max_context_chars=_integer(values, "AVITO_REPLY_MAX_CONTEXT_CHARS", 4000, minimum=500, maximum=12000),
            reply_timeout_seconds=_integer(values, "AVITO_REPLY_TIMEOUT_SECONDS", 15, minimum=1, maximum=60),
            http_timeout_seconds=_integer(values, "AVITO_HTTP_TIMEOUT_SECONDS", 10, minimum=1, maximum=60),
            debounce_seconds=8,
            diagnostic_retention_days=_integer(
                values, "AVITO_DIAGNOSTIC_RETENTION_DAYS", 30, minimum=1, maximum=90
            ),
            processing_lease_seconds=_integer(
                values, "AVITO_PROCESSING_LEASE_SECONDS", 60, minimum=30, maximum=300
            ),
            send_reconcile_grace_seconds=_integer(
                values, "AVITO_SEND_RECONCILE_GRACE_SECONDS", 120, minimum=30, maximum=600
            ),
        )
        if settings.processing_enabled:
            missing = [
                name for name, value in (
                    ("AVITO_CLIENT_ID", settings.client_id),
                    ("AVITO_CLIENT_SECRET", settings.client_secret),
                    ("AVITO_ACCOUNT_USER_ID", settings.account_user_id),
                    ("AVITO_ALLOWED_ITEM_IDS", settings.allowed_item_ids),
                    ("AVITO_WEBHOOK_SECRET", settings.webhook_secret),
                    ("OPENAI_API_KEY", settings.openai_api_key),
                ) if not value
            ]
            if missing:
                raise ValueError("Avito observe/live mode requires " + ", ".join(missing))
        if settings.webhook_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Avito webhook listener must bind to loopback")
        return settings

    def require_runtime_webhook(self) -> None:
        if not _valid_webhook_secret(self.webhook_secret):
            raise ValueError("Avito webhook runtime requires AVITO_WEBHOOK_SECRET")


def _integer(
    values: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int | None = None
) -> int:
    raw = values.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is outside the allowed range")
    return value


def _integer_list(raw: str) -> tuple[int, ...]:
    if not raw.strip():
        return ()
    try:
        values = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ValueError("AVITO_ALLOWED_ITEM_IDS must contain comma-separated integers") from exc
    if not values or any(value <= 0 for value in values):
        raise ValueError("AVITO_ALLOWED_ITEM_IDS must contain positive identifiers")
    return values


def _valid_webhook_secret(value: str) -> bool:
    return len(value) == 43 and all(character.isalnum() or character in "-_" for character in value)
