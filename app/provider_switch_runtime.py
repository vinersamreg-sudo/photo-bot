"""Linux host boundary for provider ops; SQLite is opened strictly read-only."""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from app.config import load_settings
from app.database import CURRENT_SCHEMA_VERSION, ReadOnlyDatabase, schema_version
from app.direct_prompt import build_direct_prompt
from app.max_transport import MaxApiClient
from app.payment_admin import payment_reconciliation_summary
from app.provider_router import select_image_provider
from app.provider_switch import Environment, SUPPORTED_MODELS, SwitchBlocked


SMOKE_PROMPT = "  На фото 1 добавь синий круг из фото 2 справа от красного квадрата.\nБелый фон. Тест 🔵 — без текста.  "


def settings_from(environment: Environment, root: Path):
    values = environment.values()
    values.setdefault("BASE_DIR", str(root))
    return load_settings(environ=values)


def processing_enabled(settings) -> bool:
    # Respect the existing DemoService budget switch; never rewrite it to force a switch.
    return settings.openai_image_requests_enabled and not (
        settings.image_provider == "openai" and settings.openai_balance_usd is not None
        and settings.openai_balance_usd <= settings.openai_balance_critical_usd)


def database_snapshot(path: Path) -> dict:
    database = ReadOnlyDatabase(path)
    with database.read() as connection:
        connection.execute("BEGIN")
        quick = connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        version = schema_version(connection)
        attempts = connection.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE status IN ('pending','processing')"
        ).fetchone()[0]
        dialogs = connection.execute("SELECT COUNT(*) FROM max_dialogs WHERE state='processing'").fetchone()[0]
        reservations = connection.execute(
            "SELECT COUNT(*) FROM generation_credit_reservations WHERE status='reserved'"
        ).fetchone()[0]
        account_reserved = connection.execute(
            "SELECT COALESCE(SUM(reserved_generation_credits),0) FROM user_credit_accounts"
        ).fetchone()[0]
        lot_reserved = connection.execute(
            "SELECT COALESCE(SUM(reserved_credits),0) FROM generation_credit_lots"
        ).fetchone()[0]
        credit_mismatches = connection.execute(
            """SELECT COUNT(*) FROM user_credit_accounts a WHERE
               a.reserved_generation_credits <> (SELECT COALESCE(SUM(l.reserved_credits),0)
                 FROM generation_credit_lots l WHERE l.user_id=a.user_id)
               OR a.available_generation_credits <> (SELECT COALESCE(SUM(l.available_credits),0)
                 FROM generation_credit_lots l WHERE l.user_id=a.user_id)"""
        ).fetchone()[0]
        row = connection.execute(
            "SELECT updated_at FROM max_transport_state WHERE name='poll_last_success'"
        ).fetchone()
    reconciliation = payment_reconciliation_summary(database)
    return dict(quick_check=quick, schema=version, processing=attempts, processing_dialogs=dialogs,
                reserved_credits=max(reservations, account_reserved, lot_reserved),
                credit_mismatches=credit_mismatches, reconciliation=reconciliation["status"],
                poll_success_at=str(row[0]) if row else None)


def _command(*args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout).stdout


class Runtime:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.env_path = self.root / ".env"

    def environment(self):
        return Environment.read(self.env_path)

    def _host(self):
        fields = ("ActiveState", "SubState", "MainPID", "NRestarts", "ExecMainStartTimestampMonotonic")
        output = _command("systemctl", "show", "photo-bot.service", "--property=" + ",".join(fields))
        status = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        pid = int(status["MainPID"])
        active = status["ActiveState"] == "active" and status["SubState"] == "running" and pid > 0
        runtime_values, count = {}, 0
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                args = (process / "cmdline").read_bytes().split(b"\0")
                if any(args[i:i+3] == [b"-m", b"app.main", b"run"] for i in range(len(args))):
                    count += 1
            except (FileNotFoundError, ProcessLookupError):
                continue
        if active:
            process = Path("/proc") / str(pid)
            if (process / "cwd").resolve() != self.root:
                raise SwitchBlocked("unexpected_service_root")
            runtime_values = dict(item.decode().split("=", 1) for item in
                                  (process / "environ").read_bytes().split(b"\0") if b"=" in item)
        started = time.time() - time.monotonic() + int(status["ExecMainStartTimestampMonotonic"]) / 1e6
        return dict(active=active, pid=pid, runtime_count=count, restarts=int(status["NRestarts"]),
                    started=started), runtime_values

    def snapshot(self, *, online: bool):
        environment = self.environment()
        configured = settings_from(environment, self.root)
        host, runtime_values = self._host()
        runtime_values.setdefault("BASE_DIR", str(self.root))
        active = load_settings(environ=runtime_values)
        provider = active.image_provider if host["active"] and active.image_provider in SUPPORTED_MODELS else "unknown"
        raw_model = active.gemini_image_model if provider == "gemini" else active.openai_image_model
        model = raw_model if raw_model in SUPPORTED_MODELS.get(provider, ()) else "unsupported"
        matches = host["active"] and all(runtime_values.get(k) == v for k, v in environment.values().items())
        data = database_snapshot(configured.database_path)
        poll_timestamp = datetime.fromisoformat(data.pop("poll_success_at") or "1970-01-01T00:00:00+00:00")
        if poll_timestamp.tzinfo is None:
            poll_timestamp = poll_timestamp.replace(tzinfo=timezone.utc)
        age = time.time() - poll_timestamp.timestamp()
        fresh = 0 <= age <= configured.max_poll_max_stale_seconds and poll_timestamp.timestamp() >= host["started"]
        api_ok = None
        if online:
            api_ok = False
            ca = Path(configured.max_ca_bundle) if configured.max_ca_bundle else None
            if ca and not ca.is_absolute():
                ca = self.root / ca
            client = MaxApiClient(configured.max_bot_token, configured.max_api_base_url,
                                  timeout_seconds=10, ca_bundle=ca)
            try:
                client.get_me()  # No GET /updates, customer messages or polling cursor writes.
                api_ok = client.last_status_code == 200
            except Exception:
                pass
            finally:
                client.close()
        source = (self.root / ".deploy-sha").read_text().strip()
        completed = (self.root / "data" / "deployed_commit.txt").read_text().strip()
        complete = source == completed and len(source) == 40 and all(c in "0123456789abcdef" for c in source)
        idle = data["processing"] == data["processing_dialogs"] == data["reserved_credits"] == 0
        healthy = bool(host["active"] and host["runtime_count"] == 1 and matches and complete
                       and provider in SUPPORTED_MODELS
                       and model in SUPPORTED_MODELS[provider]
                       and active.image_direct_prompt_enabled
                       and processing_enabled(active)
                       and active.max_transport_mode == "polling" and fresh and api_ok is not False
                       and data["quick_check"] and data["schema"] == CURRENT_SCHEMA_VERSION
                       and data["reconciliation"] == "OK" and data["credit_mismatches"] == 0)
        return dict(provider=provider, model=model, direct_prompt=active.image_direct_prompt_enabled,
                    service_active=host["active"], pid=host["pid"], restarts=host["restarts"],
                    runtime_count=host["runtime_count"], max_polling_fresh=fresh,
                    max_poll_age_seconds=round(max(0, age)), max_api_ok=api_ok,
                    config_matches_runtime=bool(matches), complete_deploy=complete,
                    healthy=healthy, idle=idle, **data)

    def idle(self):
        data = database_snapshot(settings_from(self.environment(), self.root).database_path)
        return (data["processing"] == data["processing_dialogs"] == data["reserved_credits"] == 0
                and data["quick_check"] and data["schema"] == CURRENT_SCHEMA_VERSION
                and data["reconciliation"] == "OK" and data["credit_mismatches"] == 0)

    def restart(self):
        _command("systemctl", "restart", "photo-bot.service", timeout=60)

    def wait_healthy(self, provider, model, *, previous_pid=None):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                status = self.snapshot(online=True)
                if status["restarts"] != 0:
                    return False  # No automatic systemd restart loop accepted.
                if (status["healthy"] and status["idle"] and status["provider"] == provider
                        and status["model"] == model and status["pid"] != previous_pid):
                    return True
            except Exception:
                pass
            time.sleep(3)  # Bounded readiness reads only, never restart/image retries.
        return False

    def smoke(self, environment):
        settings = replace(settings_from(environment, self.root), openai_max_retries=0)
        return synthetic_smoke(settings)


def synthetic_smoke(settings, *, temporary_root: Path = Path("/var/tmp")) -> dict:
    """Exactly one adapter invocation. HTTP hook proves image order and prompt on the wire."""
    started = time.monotonic()
    observed = {"requests": 0, "exact_prompt": False, "ordered_images": False, "http": None}
    client = None
    try:
        if not settings.image_direct_prompt_enabled or not processing_enabled(settings):
            raise SwitchBlocked("target_processing_disabled")
        with tempfile.TemporaryDirectory(prefix="ravuna-provider-smoke-", dir=temporary_root) as directory:
            paths = tuple(Path(directory) / name for name in ("1-square.png", "2-circle.png"))
            for index, path in enumerate(paths):
                image = Image.new("RGB", (256, 256), "white")
                draw = ImageDraw.Draw(image)
                if index == 0:
                    draw.rectangle((40, 40, 160, 160), fill="red")
                else:
                    draw.ellipse((60, 60, 190, 190), fill="blue")
                image.save(path)
            expected = [path.read_bytes() for path in paths]
            prompt = build_direct_prompt(SMOKE_PROMPT)

            def observe(request):
                observed["requests"] += 1
                if observed["requests"] != 1:
                    raise SwitchBlocked("unexpected_smoke_retry")
                body = request.read()
                if settings.image_provider == "gemini":
                    payload = json.loads(body)
                    parts = payload["input"]
                    texts = [part["text"] for part in parts if part["type"] == "text"]
                    images = [base64.b64decode(part["data"]) for part in parts if part["type"] == "image"]
                    expected_path = "/v1beta/interactions"
                else:
                    message = BytesParser(policy=policy.default).parsebytes(
                        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + body)
                    parts = list(message.iter_parts())
                    texts = [part.get_payload(decode=True).decode() for part in parts
                             if part.get_param("name", header="content-disposition") == "prompt"]
                    images = [part.get_payload(decode=True) for part in parts
                              if part.get_filename() is not None]
                    expected_path = "/v1/images/edits"
                observed["exact_prompt"] = texts == [SMOKE_PROMPT]
                observed["ordered_images"] = images == expected
                if not observed["exact_prompt"] or not observed["ordered_images"] or request.url.path != expected_path:
                    raise SwitchBlocked("smoke_request_contract_failed")

            def response_hook(response):
                observed["http"] = response.status_code

            hooks = {"request": [observe], "response": [response_hook]}
            if settings.image_provider == "openai":
                from openai import OpenAI
                if not settings.openai_api_key:
                    raise SwitchBlocked("target_credential_missing")
                client = OpenAI(api_key=settings.openai_api_key, max_retries=0,
                                timeout=settings.generation_timeout_seconds,
                                http_client=httpx.Client(event_hooks=hooks))
                provider = select_image_provider(settings, openai_client=client).provider
            elif settings.image_provider == "gemini":
                from app.gemini_image_provider import create_gemini_client
                client = create_gemini_client(settings.gemini_api_key, settings.generation_timeout_seconds)
                client.event_hooks = hooks
                provider = select_image_provider(settings, gemini_client=client).provider
            else:
                raise SwitchBlocked("unsupported_smoke_provider")
            result = provider.edit_many(paths, prompt)
            with Image.open(io.BytesIO(result.image_bytes)) as image:
                image.verify()
            ok = (result.retries == 0 and observed["requests"] == 1 and observed["http"] == 200
                  and observed["exact_prompt"] and observed["ordered_images"])
            return dict(ok=bool(ok), category="ok" if ok else "invalid_result_contract",
                        latency_seconds=round(time.monotonic() - started, 3), **observed)
    except Exception as exc:
        # Static categories only; never str(exc), remote code/message/request ID.
        names = {"ProviderTimeoutError": "timeout", "APITimeoutError": "timeout",
                 "ProviderQuotaError": "quota", "PolicyRejectedError": "policy",
                 "ProviderInvalidRequestError": "invalid_request",
                 "ProviderUnavailableError": "unavailable"}
        return dict(ok=False, category=names.get(type(exc).__name__, "smoke_failed"),
                    latency_seconds=round(time.monotonic() - started, 3), **observed)
    finally:
        if client is not None:
            client.close()
