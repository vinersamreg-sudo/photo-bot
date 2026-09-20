"""Operator entrypoint for the isolated Avito first responder."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from .api import AvitoApiClient
from .config import AvitoResponderSettings
from .repository import AvitoRepository
from .responder import AvitoReplyGenerator
from .service import AvitoResponderService, _reply_context
from .models import ChatMessage
from .webhook import AvitoWebhookServer


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ravuna avito")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("status")
    dry_run = commands.add_parser("dry-run")
    dry_run.add_argument("--fixture", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    settings = AvitoResponderSettings.from_environment(PROJECT_ROOT)
    repository = AvitoRepository(
        settings.database_path,
        debounce_seconds=8,
        lease_seconds=settings.processing_lease_seconds,
    )
    if args.command == "status":
        jobs = repository.status() if settings.database_path.exists() else {}
        print(json.dumps({
            "mode": settings.mode,
            "enabled": settings.auto_reply_enabled,
            "processing_enabled": settings.processing_enabled,
            "send_enabled": settings.auto_reply_enabled,
            "allowed_item_count": len(settings.allowed_item_ids),
            "debounce_seconds": 8,
            "model": settings.reply_model,
            "jobs": jobs,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    repository.initialize()
    if args.command == "dry-run":
        if not settings.openai_api_key:
            raise SystemExit("OPENAI_API_KEY is required for Avito dry-run")
        values = json.loads(args.fixture.read_text(encoding="utf-8"))
        messages = tuple(ChatMessage.from_api(value) for value in values["messages"])
        context = _reply_context(
            "dry-run", int(values.get("item_id") or 0), messages,
            message_limit=settings.context_messages,
            char_limit=settings.max_context_chars,
        )
        client = OpenAI(
            api_key=settings.openai_api_key, max_retries=0,
            timeout=settings.reply_timeout_seconds,
        )
        generated = AvitoReplyGenerator(
            client.responses, settings.reply_model,
            timeout_seconds=settings.reply_timeout_seconds,
        ).generate(context)
        print(json.dumps({
            "status": "dry_run",
            "sent": False,
            "image_count": context.image_count,
            "reply": generated.text,
            "model": settings.reply_model,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    settings.require_runtime_webhook()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if settings.processing_enabled:
        api = AvitoApiClient(
            settings.client_id, settings.client_secret, settings.api_base_url,
            timeout_seconds=settings.http_timeout_seconds,
            allow_send=settings.auto_reply_enabled,
        )
        openai_client = OpenAI(
            api_key=settings.openai_api_key,
            max_retries=0,
            timeout=settings.reply_timeout_seconds,
        )
        generator = AvitoReplyGenerator(
            openai_client.responses, settings.reply_model,
            timeout_seconds=settings.reply_timeout_seconds,
        )
    else:
        api = _DisabledApi()
        generator = _DisabledGenerator()
    service = AvitoResponderService(settings, repository, api, generator)
    server = AvitoWebhookServer(
        service, settings.webhook_host, settings.webhook_port, settings.webhook_path
    )
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_args: stop.set())
    server.start()
    try:
        service.run_worker(stop)
    finally:
        server.stop()
        api.close()
    return 0


class _DisabledApi:
    def close(self) -> None:
        return None

    def __getattr__(self, name: str):
        raise RuntimeError("Avito API access is disabled")


class _DisabledGenerator:
    def generate(self, context):
        raise RuntimeError("Avito reply generation is disabled")


if __name__ == "__main__":
    raise SystemExit(main())
