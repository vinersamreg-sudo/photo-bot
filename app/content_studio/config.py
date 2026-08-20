"""Fail-closed Content Studio configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ContentStudioSettings:
    base_dir: Path
    database_path: Path
    storage_dir: Path
    approved_assets_dir: Path
    publishing_enabled: bool = False
    bot_url: str = "https://max.ru/se13572368_bot"
    max_publishing_enabled: bool = False
    max_channel_id: str = ""
    max_bot_token: str = field(default="", repr=False)
    max_api_base_url: str = "https://platform-api2.max.ru"
    telegram_publishing_enabled: bool = False
    telegram_channel_id: str = ""
    telegram_bot_token: str = field(default="", repr=False)
    vk_publishing_enabled: bool = False
    vk_group_id: str = ""
    vk_access_token: str = field(default="", repr=False)
    vk_token_type: str = "user"
    vk_api_version: str = "5.199"
    full_auto_enabled: bool = False
    content_library_path: Path = Path("marketing/content/library.json")
    production_database_path: Path = Path("data/photo_bot.sqlite3")
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    minimum_queue_days: int = 7
    max_daily_publish_time: str = "19:00"
    vk_video_publish_time: str = "20:00"
    vk_wall_publish_time: str = "18:30"
    exploration_rate: float = 0.20
    daily_publish_time: str = "19:00"
    daily_publish_timezone: str = "Europe/Samara"

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
            values.get("CONTENT_STUDIO_PUBLISHING_ENABLED", "false"),
            "CONTENT_STUDIO_PUBLISHING_ENABLED",
        )
        bot_url = (
            values.get("CONTENT_STUDIO_BOT_URL", "https://max.ru/se13572368_bot").strip()
            or "https://max.ru/se13572368_bot"
        )
        parsed = urlsplit(bot_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("CONTENT_STUDIO_BOT_URL must be an absolute HTTPS URL")
        resolved = base_dir.resolve()
        approved_raw = values.get("CONTENT_STUDIO_APPROVED_ASSETS_DIR", "").strip()
        approved_assets_dir = (
            Path(approved_raw).expanduser()
            if approved_raw
            else project_root / "marketing" / "assets" / "approved"
        )
        if not approved_assets_dir.is_absolute():
            approved_assets_dir = project_root / approved_assets_dir
        approved_assets_dir = approved_assets_dir.resolve()
        customer_data_root = (project_root / "data").resolve()
        if _inside(approved_assets_dir, customer_data_root) or _inside(
            approved_assets_dir, resolved
        ):
            raise ValueError(
                "CONTENT_STUDIO_APPROVED_ASSETS_DIR must be outside customer and runtime storage"
            )
        daily_publish_time = values.get(
            "CONTENT_STUDIO_DAILY_PUBLISH_TIME", "19:00"
        ).strip()
        _validate_clock_time(daily_publish_time)
        max_publish_time = values.get(
            "CONTENT_STUDIO_MAX_DAILY_PUBLISH_TIME", daily_publish_time
        ).strip()
        vk_video_time = values.get(
            "CONTENT_STUDIO_VK_VIDEO_PUBLISH_TIME", "20:00"
        ).strip()
        vk_wall_time = values.get(
            "CONTENT_STUDIO_VK_WALL_PUBLISH_TIME", "18:30"
        ).strip()
        for value in (max_publish_time, vk_video_time, vk_wall_time):
            _validate_clock_time(value)
        library_raw = values.get(
            "CONTENT_STUDIO_LIBRARY_PATH", "marketing/content/library.json"
        ).strip()
        content_library_path = Path(library_raw).expanduser()
        if not content_library_path.is_absolute():
            content_library_path = project_root / content_library_path
        production_db_raw = values.get(
            "CONTENT_STUDIO_PRODUCTION_DATABASE_PATH", "data/photo_bot.sqlite3"
        ).strip()
        production_database_path = Path(production_db_raw).expanduser()
        if not production_database_path.is_absolute():
            production_database_path = project_root / production_database_path
        minimum_queue_days = _positive_int(
            values.get("CONTENT_STUDIO_MINIMUM_QUEUE_DAYS", "7"),
            "CONTENT_STUDIO_MINIMUM_QUEUE_DAYS",
            maximum=30,
        )
        exploration_rate = _fraction(
            values.get("CONTENT_STUDIO_EXPLORATION_RATE", "0.20"),
            "CONTENT_STUDIO_EXPLORATION_RATE",
        )
        return cls(
            base_dir=resolved,
            database_path=resolved / "content_studio.sqlite3",
            storage_dir=resolved / "storage",
            approved_assets_dir=approved_assets_dir,
            publishing_enabled=publishing_enabled,
            bot_url=bot_url,
            max_publishing_enabled=_boolean(
                values.get("CONTENT_STUDIO_MAX_PUBLISHING_ENABLED", "false"),
                "CONTENT_STUDIO_MAX_PUBLISHING_ENABLED",
            ),
            max_channel_id=values.get("CONTENT_STUDIO_MAX_CHANNEL_ID", "").strip(),
            max_bot_token=values.get("CONTENT_STUDIO_MAX_BOT_TOKEN", "").strip(),
            max_api_base_url=(
                values.get("CONTENT_STUDIO_MAX_API_BASE_URL", "https://platform-api2.max.ru").strip()
                or "https://platform-api2.max.ru"
            ),
            telegram_publishing_enabled=_boolean(
                values.get("CONTENT_STUDIO_TELEGRAM_PUBLISHING_ENABLED", "false"),
                "CONTENT_STUDIO_TELEGRAM_PUBLISHING_ENABLED",
            ),
            telegram_channel_id=values.get(
                "CONTENT_STUDIO_TELEGRAM_CHANNEL_ID", ""
            ).strip(),
            telegram_bot_token=values.get(
                "CONTENT_STUDIO_TELEGRAM_BOT_TOKEN", ""
            ).strip(),
            vk_publishing_enabled=_boolean(
                values.get("CONTENT_STUDIO_VK_PUBLISHING_ENABLED", "false"),
                "CONTENT_STUDIO_VK_PUBLISHING_ENABLED",
            ),
            vk_group_id=values.get("CONTENT_STUDIO_VK_GROUP_ID", "").strip(),
            vk_access_token=values.get("CONTENT_STUDIO_VK_ACCESS_TOKEN", "").strip(),
            vk_token_type=values.get("CONTENT_STUDIO_VK_TOKEN_TYPE", "user").strip().lower()
            or "user",
            vk_api_version=values.get("CONTENT_STUDIO_VK_API_VERSION", "5.199").strip()
            or "5.199",
            full_auto_enabled=_boolean(
                values.get("CONTENT_STUDIO_FULL_AUTO_ENABLED", "false"),
                "CONTENT_STUDIO_FULL_AUTO_ENABLED",
            ),
            content_library_path=content_library_path.resolve(),
            production_database_path=production_database_path.resolve(),
            ffmpeg_binary=values.get("CONTENT_STUDIO_FFMPEG_BINARY", "ffmpeg").strip()
            or "ffmpeg",
            ffprobe_binary=values.get("CONTENT_STUDIO_FFPROBE_BINARY", "ffprobe").strip()
            or "ffprobe",
            minimum_queue_days=minimum_queue_days,
            max_daily_publish_time=max_publish_time,
            vk_video_publish_time=vk_video_time,
            vk_wall_publish_time=vk_wall_time,
            exploration_rate=exploration_rate,
            daily_publish_time=daily_publish_time,
            daily_publish_timezone=values.get(
                "CONTENT_STUDIO_DAILY_PUBLISH_TIMEZONE", "Europe/Samara"
            ).strip()
            or "Europe/Samara",
        )


def _boolean(raw: str, name: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_clock_time(value: str) -> None:
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("CONTENT_STUDIO_DAILY_PUBLISH_TIME must use HH:MM")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("CONTENT_STUDIO_DAILY_PUBLISH_TIME must use HH:MM")


def _positive_int(raw: str, name: str, *, maximum: int) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _fraction(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if not 0.20 <= value <= 0.50:
        raise ValueError(f"{name} must be between 0.20 and 0.50")
    return value
