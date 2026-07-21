"""Fail-closed Content Studio configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ContentStudioSettings:
    base_dir: Path
    database_path: Path
    storage_dir: Path
    publishing_enabled: bool = False
    bot_url: str = "https://max.ru/se13572368_bot"

    @classmethod
    def from_environment(
        cls,
        project_root: Path,
        environ: Mapping[str, str] | None = None,
    ) -> "ContentStudioSettings":
        values = os.environ if environ is None else environ
        base_raw = values.get("CONTENT_STUDIO_BASE_DIR", "").strip()
        base_dir = Path(base_raw).expanduser() if base_raw else project_root / "data" / "content_studio"
        if not base_dir.is_absolute():
            base_dir = project_root / base_dir
        publishing_enabled = _boolean(
            values.get("CONTENT_STUDIO_PUBLISHING_ENABLED", "false")
        )
        bot_url = (
            values.get("CONTENT_STUDIO_BOT_URL", "https://max.ru/se13572368_bot").strip()
            or "https://max.ru/se13572368_bot"
        )
        parsed = urlsplit(bot_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("CONTENT_STUDIO_BOT_URL must be an absolute HTTPS URL")
        resolved = base_dir.resolve()
        return cls(
            base_dir=resolved,
            database_path=resolved / "content_studio.sqlite3",
            storage_dir=resolved / "storage",
            publishing_enabled=publishing_enabled,
            bot_url=bot_url,
        )


def _boolean(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError("CONTENT_STUDIO_PUBLISHING_ENABLED must be a boolean")
