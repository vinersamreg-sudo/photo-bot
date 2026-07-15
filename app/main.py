"""Command-line entry point for operational checks and the process skeleton."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List
from uuid import uuid4

from app.config import Settings, load_settings
from app.openai_client import (
    OpenAICheckError,
    OpenAIConfigurationError,
    check_openai_connection,
    create_openai_client,
)
from app.database import Database
from app.domain import DemoError
from app.image_service import build_demo_service
from app.stats import collect_demo_stats
from app.gallery import GalleryService
from app.storage import PrivateStorage


LOGGER = logging.getLogger(__name__)
MINIMUM_PYTHON = (3, 12, 0)


class SecretRedactionFilter(logging.Filter):
    """Remove configured secret values from log messages and arguments."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if secret)

    def _redact(self, value: object) -> object:
        if not isinstance(value, str):
            return value
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(item) for item in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._redact(value) for key, value in record.args.items()}
        return True


def configure_logging(settings: Settings) -> None:
    """Configure console logging and a project-local/production log file."""

    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if settings.logs_dir.is_dir():
        handlers.append(logging.FileHandler(settings.log_file, encoding="utf-8"))

    redaction_filter = SecretRedactionFilter(
        [settings.openai_api_key, settings.max_bot_token]
    )
    for handler in handlers:
        handler.addFilter(redaction_filter)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _directory_is_writable(path: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".health-", delete=True):
            pass
        return True
    except OSError:
        return False


def health_errors(settings: Settings) -> List[str]:
    """Return all filesystem and runtime health problems."""

    errors: List[str] = []
    if sys.version_info[:3] < MINIMUM_PYTHON:
        errors.append(
            "Python 3.12 or newer is required; found "
            + ".".join(str(part) for part in sys.version_info[:3])
        )

    for name, path in (
        ("data", settings.data_dir),
        ("logs", settings.logs_dir),
        ("temp", settings.temp_dir),
    ):
        if not path.is_dir():
            errors.append(f"Required directory is missing: {path}")
        elif not _directory_is_writable(path):
            errors.append(f"Required directory is not writable: {path}")
        else:
            LOGGER.info("Directory check passed: %s (%s)", name, path)
    if settings.max_transport_mode == "polling":
        errors.extend(_polling_health_errors(settings))
    return errors


def _systemd_runtime_status() -> tuple[bool, int]:
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "photo-bot.service"],
            check=False,
        ).returncode == 0
        result = subprocess.run(
            ["systemctl", "show", "photo-bot.service", "--property=MainPID", "--value"],
            check=False,
            capture_output=True,
            text=True,
        )
        pid = int(result.stdout.strip() or "0") if result.returncode == 0 else 0
        return active, pid
    except (OSError, ValueError):
        return False, 0


def _polling_health_errors(settings: Settings) -> List[str]:
    from app.max_transport import MaxTransportError, SingleInstanceLock

    errors: List[str] = []
    if not settings.max_bot_token:
        errors.append("MAX configuration missing: MAX_BOT_TOKEN")
    try:
        connection = sqlite3.connect(
            f"file:{settings.database_path}?mode=rw", uri=True, timeout=1
        )
        try:
            check = connection.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                errors.append("SQLite integrity check failed")
            row = connection.execute(
                "SELECT updated_at FROM max_transport_state WHERE name='poll_last_success'"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.OperationalError as exc:
        kind = "sqlite_lock" if "locked" in str(exc).lower() else "sqlite_unavailable"
        errors.append(f"MAX database health failed ({kind})")
        row = None
    if row is None:
        errors.append("MAX polling has no successful contact timestamp")
    else:
        try:
            updated_at = datetime.fromisoformat(str(row[0]))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if age < 0 or age > settings.max_poll_max_stale_seconds:
                errors.append(
                    f"MAX polling contact is stale ({int(max(age, 0))}s)"
                )
        except (TypeError, ValueError):
            errors.append("MAX polling contact timestamp is invalid")

    if settings.app_env == "production":
        active, pid = _systemd_runtime_status()
        if not active:
            errors.append("photo-bot.service is not active")
        if pid <= 0:
            errors.append("photo-bot.service has no live MainPID")
        else:
            try:
                command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
                if b"-m app.main run" not in command:
                    errors.append("photo-bot.service MainPID command is unexpected")
            except OSError:
                errors.append("photo-bot.service MainPID is not readable")
        try:
            with SingleInstanceLock(settings.max_poll_lock_path):
                errors.append("MAX polling runtime lock is not held")
        except MaxTransportError as exc:
            if exc.kind != "duplicate_polling_instance":
                errors.append(f"MAX polling lock check failed ({exc.kind})")
    return errors


def run_health(settings: Settings) -> int:
    errors = health_errors(settings)
    if errors:
        for error in errors:
            LOGGER.error("%s", error)
        return 1
    LOGGER.info(
        "Healthcheck passed (environment=%s, Python=%s)",
        settings.app_env,
        sys.version.split()[0],
    )
    return 0


def run_openai_check(settings: Settings) -> int:
    try:
        client = create_openai_client(settings)
        check_openai_connection(client, settings.openai_image_model)
    except (OpenAIConfigurationError, OpenAICheckError) as exc:
        LOGGER.error("%s", exc)
        return 1
    LOGGER.info("OpenAI client creation and authorization check passed")
    return 0


def run_process(settings: Settings) -> int:
    """Run the configured MAX handler; disabled mode intentionally exits."""

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s; stopping photo-bot", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if settings.max_transport_mode == "disabled":
        LOGGER.error(
            "MAX transport is disabled; refusing to keep an idle production process"
        )
        return 2
    if settings.max_transport_mode == "webhook":
        LOGGER.error(
            "Webhook runtime requires the pending HTTPS:443 endpoint configuration"
        )
        return 2
    from app.max_runtime import run_polling
    LOGGER.info("photo-bot MAX polling process starting (pid=%s)", os.getpid())
    try:
        return run_polling(settings, stop_event)
    except Exception as exc:
        from app.max_transport import MaxTransportError
        if isinstance(exc, MaxTransportError):
            LOGGER.error(
                "MAX runtime stopped (kind=%s,http_status=%s): %s",
                exc.kind,
                exc.http_status,
                exc,
            )
            return 3
        raise


def run_max_check(settings: Settings) -> int:
    from app.max_transport import MaxApiClient, MaxTransportError
    try:
        client = MaxApiClient(
            settings.max_bot_token,
            settings.max_api_base_url,
            timeout_seconds=30,
            media_host_suffixes=settings.max_media_host_suffixes,
        )
        try:
            bot = client.get_me()
        finally:
            client.close()
    except MaxTransportError as exc:
        LOGGER.error(
            "MAX authorization check failed (kind=%s,http_status=%s): %s",
            exc.kind,
            exc.http_status,
            exc,
        )
        return 1
    bot_id = bot.get("user_id") or bot.get("bot_id")
    username = bot.get("username")
    LOGGER.info(
        "MAX bot authorization check passed (http_status=%s,bot_id=%s,username=%s)",
        client.last_status_code,
        bot_id if bot_id is not None else "not-returned",
        username if username else "not-returned",
    )
    return 0


def run_demo_edit(settings: Settings, args: argparse.Namespace) -> int:
    try:
        service = build_demo_service(settings, provider_name=args.provider)
        session = service.start_session("cli", args.user_id, Path(args.image))
        result = service.generate(
            session.session_id,
            args.prompt,
            args.event_id or f"cli-{uuid4().hex}",
            scenario_id=args.scenario,
        )
    except DemoError as exc:
        LOGGER.error("Demo request rejected: %s", exc)
        return 2
    except Exception as exc:
        LOGGER.error(
            "Demo generation failed without exposing provider response data (error_type=%s)",
            type(exc).__name__,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "succeeded",
                "session_id": session.session_id,
                "attempt_id": result.attempt_id,
                "demo_preview": str(result.preview_path),
                "remaining_free_corrections": result.remaining_generations,
                "original_disclosed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_demo_stats(settings: Settings) -> int:
    database = Database(settings.database_path)
    print(json.dumps(collect_demo_stats(database), ensure_ascii=False, indent=2))
    return 0


def run_gallery_cleanup(settings: Settings, execute: bool) -> int:
    database = Database(settings.database_path)
    service = GalleryService(
        database,
        PrivateStorage(settings.users_dir, settings.max_source_file_size_mb * 1024 * 1024),
        settings,
    )
    due = service.purge_due(execute=execute)
    print(json.dumps({"mode": "execute" if execute else "dry-run", "count": len(due), "gallery_item_ids": due}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photo-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("health", "openai-check", "max-check", "run", "demo-stats"):
        subparsers.add_parser(command)
    cleanup = subparsers.add_parser("gallery-cleanup")
    cleanup.add_argument("--execute", action="store_true")
    demo = subparsers.add_parser("demo-edit")
    demo.add_argument("--user-id", required=True)
    demo.add_argument("--image", required=True)
    demo.add_argument("--prompt", required=True)
    demo.add_argument("--scenario")
    demo.add_argument("--event-id")
    demo.add_argument("--provider", choices=("openai", "fake"), default="openai")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings)
    if args.command == "health":
        return run_health(settings)
    if args.command == "openai-check":
        return run_openai_check(settings)
    if args.command == "max-check":
        return run_max_check(settings)
    if args.command == "demo-edit":
        return run_demo_edit(settings, args)
    if args.command == "demo-stats":
        return run_demo_stats(settings)
    if args.command == "gallery-cleanup":
        return run_gallery_cleanup(settings, args.execute)
    return run_process(settings)


if __name__ == "__main__":
    raise SystemExit(main())
