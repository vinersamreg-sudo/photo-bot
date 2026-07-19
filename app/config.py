"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Runtime settings and resolved application paths."""

    openai_api_key: str
    openai_image_model: str
    app_env: str
    base_dir: Path
    image_edit_quality: str = "medium"
    image_edit_size: str = "1024x1024"
    image_edit_input_fidelity: str = "auto"
    image_edit_output_format: str = "png"
    openai_max_retries: int = 2
    openai_conversation_memory_enabled: bool = False
    openai_responses_image_enabled: bool = False
    openai_conversation_retention_enabled: bool = False
    openai_responses_model: str = "gpt-5.4-mini"
    openai_context_retention_days: int = 30
    openai_context_delete_on_gallery_delete: bool = True
    openai_context_delete_on_user_delete: bool = True
    openai_context_max_idle_days: int = 14
    openai_context_max_depth: int = 8
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
    payments_enabled: bool = False
    payment_provider: str = "disabled"
    payment_currency: str = "RUB"
    payment_order_ttl_minutes: int = 30
    payment_webhook_enabled: bool = False
    payment_webhook_host: str = "127.0.0.1"
    payment_webhook_port: int = 8091
    payment_webhook_path: str = "/payments/robokassa/result"
    payment_refunds_enabled: bool = False
    robokassa_mode: str = "sandbox"
    robokassa_production_approved: bool = False
    robokassa_commission_percent: float = 0.0
    robokassa_merchant_login: str = ""
    robokassa_password1: str = ""
    robokassa_password2: str = ""
    robokassa_password3: str = ""
    robokassa_hash_algorithm: str = "sha256"
    robokassa_payment_url: str = "https://auth.robokassa.ru/Merchant/Index.aspx"
    robokassa_refund_url: str = "https://services.robokassa.ru/RefundService/Refund/Create"
    robokassa_refund_status_url: str = "https://services.robokassa.ru/RefundService/Refund/GetState"
    payment_result_url: str = ""
    payment_success_url: str = ""
    payment_fail_url: str = ""
    payment_receipt_tax: str = "none"
    payment_receipt_item_name: str = "Оригинал фотографии Pixora"
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
    max_owner_user_ids: tuple[str, ...] = ()
    max_pilot_user_ids: tuple[str, ...] = ()
    pilot_user_limit: int = 0
    max_media_host_suffixes: tuple[str, ...] = (".max.ru", ".oneme.ru", ".okcdn.ru")
    processing_mode_router_enabled: bool = False
    real_background_composite_enabled: bool = False
    allow_ai_background_fallback: bool = False
    background_asset_catalog: str = "assets/backgrounds/catalog.json"
    segmentation_backend: str = "disabled"
    segmentation_model: str = "u2net_human_seg"
    segmentation_model_dir: str = "data/models/rembg"
    segmentation_model_sha256: str = ""
    segmentation_timeout_seconds: int = 120
    local_ai_finishing_enabled: bool = False
    backup_dir: str = "data/backups"
    backup_retention_days: int = 14
    backup_max_age_hours: int = 30
    cleanup_temp_retention_hours: int = 24
    cleanup_orphan_grace_hours: int = 24
    disk_min_free_mb: int = 2048

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

    @property
    def background_asset_catalog_path(self) -> Path:
        path = Path(self.background_asset_catalog).expanduser()
        return path if path.is_absolute() else self.base_dir / path

    @property
    def segmentation_model_dir_path(self) -> Path:
        path = Path(self.segmentation_model_dir).expanduser()
        return path if path.is_absolute() else self.base_dir / path

    @property
    def backup_dir_path(self) -> Path:
        path = Path(self.backup_dir).expanduser()
        return path if path.is_absolute() else self.base_dir / path

    @property
    def max_allowed_user_ids(self) -> tuple[str, ...]:
        """Owner plus the explicitly enabled prefix of the pilot allowlist."""

        pilot = self.max_pilot_user_ids[: self.pilot_user_limit]
        return tuple(dict.fromkeys((*self.max_owner_user_ids, *pilot)))


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


def _nonnegative_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, "").strip()
    value = int(raw) if raw else default
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _nonnegative_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, "").strip()
    value = float(raw) if raw else default
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
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

    image_edit_quality = values.get("IMAGE_EDIT_QUALITY", "medium").strip().lower() or "medium"
    if image_edit_quality not in {"low", "medium", "high", "auto"}:
        raise ValueError("IMAGE_EDIT_QUALITY must be low, medium, high or auto")
    image_edit_size = values.get("IMAGE_EDIT_SIZE", "1024x1024").strip().lower() or "1024x1024"
    if image_edit_size != "auto" and not re.fullmatch(r"[1-9]\d{2,3}x[1-9]\d{2,3}", image_edit_size):
        raise ValueError("IMAGE_EDIT_SIZE must be auto or WIDTHxHEIGHT")
    image_edit_input_fidelity = (
        values.get("IMAGE_EDIT_INPUT_FIDELITY", "auto").strip().lower() or "auto"
    )
    if image_edit_input_fidelity not in {"auto", "low", "high"}:
        raise ValueError("IMAGE_EDIT_INPUT_FIDELITY must be auto, low or high")
    image_edit_output_format = (
        values.get("IMAGE_EDIT_OUTPUT_FORMAT", "png").strip().lower() or "png"
    )
    if image_edit_output_format not in {"png", "jpeg", "webp"}:
        raise ValueError("IMAGE_EDIT_OUTPUT_FORMAT must be png, jpeg or webp")

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
    segmentation_backend = (
        values.get("SEGMENTATION_BACKEND", "disabled").strip().lower() or "disabled"
    )
    if segmentation_backend not in {"disabled", "rembg"}:
        raise ValueError("SEGMENTATION_BACKEND must be disabled or rembg")
    segmentation_model_sha256 = values.get("SEGMENTATION_MODEL_SHA256", "").strip().lower()
    if segmentation_model_sha256 and (
        len(segmentation_model_sha256) != 64
        or any(char not in "0123456789abcdef" for char in segmentation_model_sha256)
    ):
        raise ValueError("SEGMENTATION_MODEL_SHA256 must be a SHA-256 hex digest")
    if segmentation_backend == "rembg" and not segmentation_model_sha256:
        raise ValueError("SEGMENTATION_MODEL_SHA256 is required for rembg")
    owner_user_ids = tuple(
        dict.fromkeys(
            part.strip()
            for part in values.get("MAX_OWNER_USER_IDS", "").split(",")
            if part.strip()
        )
    )
    pilot_user_ids = tuple(
        dict.fromkeys(
            part.strip()
            for part in values.get("MAX_PILOT_USER_IDS", "").split(",")
            if part.strip()
        )
    )
    pilot_user_limit = _nonnegative_int(values, "PILOT_USER_LIMIT", 0)
    if pilot_user_limit not in {0, 5, 10, 20}:
        raise ValueError("PILOT_USER_LIMIT must be 0, 5, 10 or 20")
    if set(owner_user_ids) & set(pilot_user_ids):
        raise ValueError("MAX pilot allowlist must not contain an owner")
    if len(pilot_user_ids) < pilot_user_limit:
        raise ValueError("MAX pilot allowlist is shorter than PILOT_USER_LIMIT")
    app_env = values.get("APP_ENV", "production").strip() or "production"
    image_model = values.get("OPENAI_IMAGE_MODEL", "").strip()
    if app_env == "production" and image_model and image_model != "gpt-image-2":
        raise ValueError("Pixora v1 production requires OPENAI_IMAGE_MODEL=gpt-image-2")
    payment_provider = values.get("PAYMENT_PROVIDER", "disabled").strip().lower() or "disabled"
    if payment_provider not in {"disabled", "robokassa"}:
        raise ValueError("PAYMENT_PROVIDER must be disabled or robokassa")
    payment_currency = values.get("PAYMENT_CURRENCY", "RUB").strip().upper() or "RUB"
    if payment_currency != "RUB":
        raise ValueError("Pixora payments currently support only RUB")
    robokassa_mode = values.get("ROBOKASSA_MODE", "sandbox").strip().lower() or "sandbox"
    if robokassa_mode not in {"sandbox", "production"}:
        raise ValueError("ROBOKASSA_MODE must be sandbox or production")
    robokassa_hash_algorithm = (
        values.get("ROBOKASSA_HASH_ALGORITHM", "sha256").strip().lower() or "sha256"
    )
    if robokassa_hash_algorithm not in {"md5", "sha256", "sha512"}:
        raise ValueError("ROBOKASSA_HASH_ALGORITHM must be md5, sha256 or sha512")
    robokassa_commission_percent = _nonnegative_float(
        values, "ROBOKASSA_COMMISSION_PERCENT", 0.0
    )
    if robokassa_commission_percent > 100:
        raise ValueError("ROBOKASSA_COMMISSION_PERCENT must be at most 100")
    payment_webhook_path = (
        values.get("PAYMENT_WEBHOOK_PATH", "/payments/robokassa/result").strip()
        or "/payments/robokassa/result"
    )
    if not payment_webhook_path.startswith("/") or "?" in payment_webhook_path:
        raise ValueError("PAYMENT_WEBHOOK_PATH must be an absolute path without a query")
    payment_webhook_port = _positive_int(values, "PAYMENT_WEBHOOK_PORT", 8091)
    if payment_webhook_port > 65535:
        raise ValueError("PAYMENT_WEBHOOK_PORT must be at most 65535")
    payments_enabled = _boolean(values, "PAYMENTS_ENABLED", False)
    webhook_enabled = _boolean(values, "PAYMENT_WEBHOOK_ENABLED", False)
    refunds_enabled = _boolean(values, "PAYMENT_REFUNDS_ENABLED", False)
    production_approved = _boolean(values, "ROBOKASSA_PRODUCTION_APPROVED", False)
    if payments_enabled and payment_provider != "robokassa":
        raise ValueError("Enabled payments require PAYMENT_PROVIDER=robokassa")
    if robokassa_mode == "production" and payments_enabled and not production_approved:
        raise ValueError("Production Robokassa requires explicit ROBOKASSA_PRODUCTION_APPROVED=true")
    if refunds_enabled and not payments_enabled:
        raise ValueError("Refunds cannot be enabled while payments are disabled")
    if payments_enabled:
        required = {
            "ROBOKASSA_MERCHANT_LOGIN": values.get("ROBOKASSA_MERCHANT_LOGIN", "").strip(),
            "ROBOKASSA_PASSWORD1": values.get("ROBOKASSA_PASSWORD1", "").strip(),
            "ROBOKASSA_PASSWORD2": values.get("ROBOKASSA_PASSWORD2", "").strip(),
            "PAYMENT_RESULT_URL": values.get("PAYMENT_RESULT_URL", "").strip(),
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise ValueError("Enabled payments are missing: " + ", ".join(missing))
        if not webhook_enabled:
            raise ValueError("Enabled payments require PAYMENT_WEBHOOK_ENABLED=true")
        if not required["PAYMENT_RESULT_URL"].startswith("https://"):
            raise ValueError("PAYMENT_RESULT_URL must use HTTPS")
        payment_provider_url = values.get(
            "ROBOKASSA_PAYMENT_URL",
            "https://auth.robokassa.ru/Merchant/Index.aspx",
        ).strip()
        parsed_payment_url = urlsplit(payment_provider_url)
        if parsed_payment_url.scheme != "https" or parsed_payment_url.hostname != "auth.robokassa.ru":
            raise ValueError("ROBOKASSA_PAYMENT_URL must use auth.robokassa.ru over HTTPS")
        if app_env == "production" and (
            values.get("PAYMENT_WEBHOOK_HOST", "127.0.0.1").strip()
            not in {"127.0.0.1", "::1", "localhost"}
        ):
            raise ValueError("Production payment webhook must bind to loopback")
    success_url = values.get("PAYMENT_SUCCESS_URL", "").strip()
    fail_url = values.get("PAYMENT_FAIL_URL", "").strip()
    if bool(success_url) != bool(fail_url):
        raise ValueError("PAYMENT_SUCCESS_URL and PAYMENT_FAIL_URL must be configured together")
    if any(url and not url.startswith("https://") for url in (success_url, fail_url)):
        raise ValueError("Payment return URLs must use HTTPS")
    if refunds_enabled and not values.get("ROBOKASSA_PASSWORD3", "").strip():
        raise ValueError("Enabled refunds require ROBOKASSA_PASSWORD3")
    if refunds_enabled:
        for name, default in (
            ("ROBOKASSA_REFUND_URL", "https://services.robokassa.ru/RefundService/Refund/Create"),
            ("ROBOKASSA_REFUND_STATUS_URL", "https://services.robokassa.ru/RefundService/Refund/GetState"),
        ):
            parsed = urlsplit(values.get(name, default).strip())
            if parsed.scheme != "https" or parsed.hostname != "services.robokassa.ru":
                raise ValueError(f"{name} must use services.robokassa.ru over HTTPS")

    return Settings(
        openai_api_key=values.get("OPENAI_API_KEY", "").strip(),
        openai_image_model=image_model,
        app_env=app_env,
        base_dir=base_dir.resolve(),
        image_edit_quality=image_edit_quality,
        image_edit_size=image_edit_size,
        image_edit_input_fidelity=image_edit_input_fidelity,
        image_edit_output_format=image_edit_output_format,
        openai_max_retries=_nonnegative_int(values, "OPENAI_MAX_RETRIES", 2),
        openai_conversation_memory_enabled=_boolean(
            values, "OPENAI_CONVERSATION_MEMORY_ENABLED", False
        ),
        openai_responses_image_enabled=_boolean(
            values, "OPENAI_RESPONSES_IMAGE_ENABLED", False
        ),
        openai_conversation_retention_enabled=_boolean(
            values, "OPENAI_CONVERSATION_RETENTION_ENABLED", False
        ),
        openai_responses_model=(
            values.get("OPENAI_RESPONSES_MODEL", "gpt-5.4-mini").strip()
            or "gpt-5.4-mini"
        ),
        openai_context_retention_days=_positive_int(
            values, "OPENAI_CONTEXT_RETENTION_DAYS", 30
        ),
        openai_context_delete_on_gallery_delete=_boolean(
            values, "OPENAI_CONTEXT_DELETE_ON_GALLERY_DELETE", True
        ),
        openai_context_delete_on_user_delete=_boolean(
            values, "OPENAI_CONTEXT_DELETE_ON_USER_DELETE", True
        ),
        openai_context_max_idle_days=_positive_int(
            values, "OPENAI_CONTEXT_MAX_IDLE_DAYS", 14
        ),
        openai_context_max_depth=_positive_int(
            values, "OPENAI_CONTEXT_MAX_DEPTH", 8
        ),
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
        payments_enabled=payments_enabled,
        payment_provider=payment_provider,
        payment_currency=payment_currency,
        payment_order_ttl_minutes=_positive_int(values, "PAYMENT_ORDER_TTL_MINUTES", 30),
        payment_webhook_enabled=webhook_enabled,
        payment_webhook_host=(
            values.get("PAYMENT_WEBHOOK_HOST", "127.0.0.1").strip() or "127.0.0.1"
        ),
        payment_webhook_port=payment_webhook_port,
        payment_webhook_path=payment_webhook_path,
        payment_refunds_enabled=refunds_enabled,
        robokassa_mode=robokassa_mode,
        robokassa_production_approved=production_approved,
        robokassa_commission_percent=robokassa_commission_percent,
        robokassa_merchant_login=values.get("ROBOKASSA_MERCHANT_LOGIN", "").strip(),
        robokassa_password1=values.get("ROBOKASSA_PASSWORD1", "").strip(),
        robokassa_password2=values.get("ROBOKASSA_PASSWORD2", "").strip(),
        robokassa_password3=values.get("ROBOKASSA_PASSWORD3", "").strip(),
        robokassa_hash_algorithm=robokassa_hash_algorithm,
        robokassa_payment_url=(
            values.get("ROBOKASSA_PAYMENT_URL", "https://auth.robokassa.ru/Merchant/Index.aspx").strip()
            or "https://auth.robokassa.ru/Merchant/Index.aspx"
        ),
        robokassa_refund_url=(
            values.get("ROBOKASSA_REFUND_URL", "https://services.robokassa.ru/RefundService/Refund/Create").strip()
            or "https://services.robokassa.ru/RefundService/Refund/Create"
        ),
        robokassa_refund_status_url=(
            values.get("ROBOKASSA_REFUND_STATUS_URL", "https://services.robokassa.ru/RefundService/Refund/GetState").strip()
            or "https://services.robokassa.ru/RefundService/Refund/GetState"
        ),
        payment_result_url=values.get("PAYMENT_RESULT_URL", "").strip(),
        payment_success_url=success_url,
        payment_fail_url=fail_url,
        payment_receipt_tax=values.get("PAYMENT_RECEIPT_TAX", "none").strip() or "none",
        payment_receipt_item_name=(
            values.get("PAYMENT_RECEIPT_ITEM_NAME", "Оригинал фотографии Pixora").strip()
            or "Оригинал фотографии Pixora"
        ),
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
        max_owner_user_ids=owner_user_ids,
        max_pilot_user_ids=pilot_user_ids,
        pilot_user_limit=pilot_user_limit,
        max_media_host_suffixes=media_suffixes,
        processing_mode_router_enabled=_boolean(
            values, "PROCESSING_MODE_ROUTER_ENABLED", False
        ),
        real_background_composite_enabled=_boolean(
            values, "REAL_BACKGROUND_COMPOSITE_ENABLED", False
        ),
        allow_ai_background_fallback=_boolean(
            values, "ALLOW_AI_BACKGROUND_FALLBACK", False
        ),
        background_asset_catalog=values.get(
            "BACKGROUND_ASSET_CATALOG", "assets/backgrounds/catalog.json"
        ).strip() or "assets/backgrounds/catalog.json",
        segmentation_backend=segmentation_backend,
        segmentation_model=values.get(
            "SEGMENTATION_MODEL", "u2net_human_seg"
        ).strip() or "u2net_human_seg",
        segmentation_model_dir=values.get(
            "SEGMENTATION_MODEL_DIR", "data/models/rembg"
        ).strip() or "data/models/rembg",
        segmentation_model_sha256=segmentation_model_sha256,
        segmentation_timeout_seconds=_positive_int(
            values, "SEGMENTATION_TIMEOUT_SECONDS", 120
        ),
        local_ai_finishing_enabled=_boolean(
            values, "LOCAL_AI_FINISHING_ENABLED", False
        ),
        backup_dir=values.get("BACKUP_DIR", "data/backups").strip() or "data/backups",
        backup_retention_days=_positive_int(values, "BACKUP_RETENTION_DAYS", 14),
        backup_max_age_hours=_positive_int(values, "BACKUP_MAX_AGE_HOURS", 30),
        cleanup_temp_retention_hours=_positive_int(
            values, "CLEANUP_TEMP_RETENTION_HOURS", 24
        ),
        cleanup_orphan_grace_hours=_positive_int(
            values, "CLEANUP_ORPHAN_GRACE_HOURS", 24
        ),
        disk_min_free_mb=_positive_int(values, "DISK_MIN_FREE_MB", 2048),
    )
