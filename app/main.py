"""Command-line entry point for operational checks and the process skeleton."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import Iterable, List

from app.config import Settings, load_settings
from app.openai_client import (
    OpenAICheckError,
    OpenAIConfigurationError,
    check_openai_connection,
    create_openai_client,
)


LOGGER = logging.getLogger(__name__)
MINIMUM_PYTHON = (3, 10, 1)


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

    redaction_filter = SecretRedactionFilter([settings.openai_api_key])
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
            "Python 3.10.1 or newer is required; found "
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
        check_openai_connection(client)
    except (OpenAIConfigurationError, OpenAICheckError) as exc:
        LOGGER.error("%s", exc)
        return 1
    LOGGER.info("OpenAI client creation and authorization check passed")
    return 0


def run_process(settings: Settings) -> int:
    """Run an idle, signal-aware process until real bot integration is added."""

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s; stopping photo-bot", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOGGER.info("photo-bot process skeleton started (pid=%s)", os.getpid())
    while not stop_event.wait(timeout=30):
        LOGGER.debug("photo-bot process skeleton is alive")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photo-bot")
    parser.add_argument("command", choices=("health", "openai-check", "run"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings)
    if args.command == "health":
        return run_health(settings)
    if args.command == "openai-check":
        return run_openai_check(settings)
    return run_process(settings)


if __name__ == "__main__":
    raise SystemExit(main())
