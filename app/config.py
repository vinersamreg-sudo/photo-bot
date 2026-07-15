"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Runtime settings and resolved application paths."""

    openai_api_key: str
    openai_image_model: str
    app_env: str
    base_dir: Path
    demo_max_successful_generations: int = 5
    demo_session_ttl_minutes: int = 60
    demo_watermark_text: str = "ОБРАЗЕЦ"
    demo_max_dimension: int = 1024
    demo_output_format: str = "JPEG"
    demo_jpeg_quality: int = 82
    demo_min_request_interval_seconds: int = 15
    demo_max_attempts_per_hour: int = 10
    demo_max_concurrent_per_user: int = 1
    global_max_concurrent_generations: int = 2
    demo_daily_cost_limit_rub: float = 1000.0
    demo_daily_generation_limit: int = 100
    demo_estimated_cost_rub_per_generation: float = 10.0
    max_source_file_size_mb: int = 15
    max_prompt_length: int = 1500
    generation_timeout_seconds: int = 300
    unlock_original_price_rub: int = 149
    demo_retention_days: int = 30
    paid_retention_days: int = 180
    trash_retention_days: int = 30
    max_bot_token: str = ""
    max_api_base_url: str = "https://platform-api2.max.ru"
    max_ca_bundle: str = "ops/certs/russian_trusted_root_ca_pem.crt"
    max_transport_mode: str = "disabled"
    max_poll_timeout_seconds: int = 20
    max_poll_retry_seconds: int = 5
    max_poll_idle_seconds: int = 1
    max_poll_max_stale_seconds: int = 90
    max_poll_observe_only: bool = True
    max_media_host_suffixes: tuple[str, ...] = (".max.ru", ".oneme.ru", ".okcdn.ru")

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def temp_dir(self) -> Path:
        return self.base_dir / "temp"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "app.log"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "photo_bot.sqlite3"

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"

    @property
    def max_poll_lock_path(self) -> Path:
        return self.data_dir / "max-polling.lock"

    @property
    def max_ca_bundle_path(self) -> Path:
        path = Path(self.max_ca_bundle).expanduser()
        return path if path.is_absolute() else self.base_dir / path


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, "").strip()
    value = int(raw) if raw else default
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, "").strip()
    value = float(raw) if raw else default
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def load_settings(
    env_file: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Load settings from an optional .env file and the process environment."""

    if environ is None:
        load_dotenv(dotenv_path=env_file or PROJECT_ROOT / ".env", override=False)
        values: Mapping[str, str] = os.environ
    else:
        values = environ

    base_dir_value = values.get("BASE_DIR", "").strip()
    base_dir = Path(base_dir_value).expanduser() if base_dir_value else PROJECT_ROOT

    output_format = values.get("DEMO_OUTPUT_FORMAT", "JPEG").strip().upper() or "JPEG"
    if output_format not in {"JPEG", "WEBP"}:
        raise ValueError("DEMO_OUTPUT_FORMAT must be JPEG or WEBP")

    max_transport_mode = values.get("MAX_TRANSPORT_MODE", "disabled").strip().lower() or "disabled"
    if max_transport_mode not in {"disabled", "polling", "webhook"}:
        raise ValueError("MAX_TRANSPORT_MODE must be disabled, polling or webhook")
    media_suffixes = tuple(
        part.strip().lower()
        for part in values.get(
            "MAX_MEDIA_HOST_SUFFIXES", ".max.ru,.oneme.ru,.okcdn.ru"
        ).split(",")
        if part.strip()
    )
    if not media_suffixes:
        raise ValueError("MAX_MEDIA_HOST_SUFFIXES must not be empty")

    return Settings(
        openai_api_key=values.get("OPENAI_API_KEY", "").strip(),
        openai_image_model=values.get("OPENAI_IMAGE_MODEL", "").strip(),
        app_env=values.get("APP_ENV", "production").strip() or "production",
        base_dir=base_dir.resolve(),
        demo_max_successful_generations=_positive_int(values, "DEMO_MAX_SUCCESSFUL_GENERATIONS", 5),
        demo_session_ttl_minutes=_positive_int(values, "DEMO_SESSION_TTL_MINUTES", 60),
        demo_watermark_text=values.get("DEMO_WATERMARK_TEXT", "ОБРАЗЕЦ").strip() or "ОБРАЗЕЦ",
        demo_max_dimension=_positive_int(values, "DEMO_MAX_DIMENSION", 1024),
        demo_output_format=output_format,
        demo_jpeg_quality=_positive_int(values, "DEMO_JPEG_QUALITY", 82),
        demo_min_request_interval_seconds=_positive_int(values, "DEMO_MIN_REQUEST_INTERVAL_SECONDS", 15),
        demo_max_attempts_per_hour=_positive_int(values, "DEMO_MAX_ATTEMPTS_PER_HOUR", 10),
        demo_max_concurrent_per_user=_positive_int(values, "DEMO_MAX_CONCURRENT_PER_USER", 1),
        global_max_concurrent_generations=_positive_int(values, "GLOBAL_MAX_CONCURRENT_GENERATIONS", 2),
        demo_daily_cost_limit_rub=_positive_float(values, "DEMO_DAILY_COST_LIMIT_RUB", 1000.0),
        demo_daily_generation_limit=_positive_int(values, "DEMO_DAILY_GENERATION_LIMIT", 100),
        demo_estimated_cost_rub_per_generation=_positive_float(values, "DEMO_ESTIMATED_COST_RUB_PER_GENERATION", 10.0),
        max_source_file_size_mb=_positive_int(values, "MAX_SOURCE_FILE_SIZE_MB", 15),
        max_prompt_length=_positive_int(values, "MAX_PROMPT_LENGTH", 1500),
        generation_timeout_seconds=_positive_int(values, "GENERATION_TIMEOUT_SECONDS", 300),
        unlock_original_price_rub=_positive_int(values, "UNLOCK_ORIGINAL_PRICE_RUB", 149),
        demo_retention_days=_positive_int(values, "DEMO_RETENTION_DAYS", 30),
        paid_retention_days=_positive_int(values, "PAID_RETENTION_DAYS", 180),
        trash_retention_days=_positive_int(values, "TRASH_RETENTION_DAYS", 30),
        max_bot_token=values.get("MAX_BOT_TOKEN", "").strip(),
        max_api_base_url=values.get(
            "MAX_API_BASE_URL", "https://platform-api2.max.ru"
        ).strip().rstrip("/"),
        max_ca_bundle=values.get(
            "MAX_CA_BUNDLE", "ops/certs/russian_trusted_root_ca_pem.crt"
        ).strip(),
        max_transport_mode=max_transport_mode,
        max_poll_timeout_seconds=_positive_int(values, "MAX_POLL_TIMEOUT_SECONDS", 20),
        max_poll_retry_seconds=_positive_int(values, "MAX_POLL_RETRY_SECONDS", 5),
        max_poll_idle_seconds=_positive_int(values, "MAX_POLL_IDLE_SECONDS", 1),
        max_poll_max_stale_seconds=_positive_int(values, "MAX_POLL_MAX_STALE_SECONDS", 90),
        max_poll_observe_only=_boolean(values, "MAX_POLL_OBSERVE_ONLY", True),
        max_media_host_suffixes=media_suffixes,
    )
