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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, List
from uuid import uuid4

from app.config import Settings, load_settings
from app.commerce import CommerceService, PRODUCT_CODE
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
from app.edit_intent import EditPlan
from app.prompt_builder import safe_prompt_inspection
from app.backup import BackupError, BackupManager
from app.maintenance import run_maintenance
from app.operations import collect_launch_status, openai_budget_status, print_launch_status
from app.provider_context import OpenAIProviderContextGateway, ProviderContextService
from app.commercial_operations import (
    backup_status,
    cleanup_status,
    cost_status,
    health_report,
    payment_status,
    pilot_status,
    storage_status,
)
from app.payment_admin import (
    mask_reference,
    payment_expiration_reconcile,
    payment_reconcile,
    payment_show,
    pilot_report,
    robokassa_health,
)
from app.payments import (
    PaymentError,
    PaymentUnavailable,
    RefundReason,
    build_payment_service,
)


LOGGER = logging.getLogger(__name__)
MINIMUM_PYTHON = (3, 12, 0)


def _human_lines(value: object, *, indent: int = 0) -> list[str]:
    """Render compact operator output without losing explicit field names."""

    prefix = "  " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, nested in value.items():
            if isinstance(nested, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.extend(_human_lines(nested, indent=indent + 1))
            else:
                lines.append(f"{prefix}{key}: {nested}")
        return lines
    if isinstance(value, list):
        lines = []
        for nested in value:
            if isinstance(nested, (dict, list)):
                lines.append(f"{prefix}-")
                lines.extend(_human_lines(nested, indent=indent + 1))
            else:
                lines.append(f"{prefix}- {nested}")
        return lines
    return [f"{prefix}{value}"]


def _print_operator(value: object, output_format: str = "json") -> None:
    if output_format == "human":
        print("\n".join(_human_lines(value)))
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


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

    # httpx logs complete request URLs at INFO. MAX callback and one-time media
    # upload credentials can be query parameters, so request-level logging must
    # remain disabled even when application INFO logging is enabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if settings.logs_dir.is_dir():
        handlers.append(logging.FileHandler(settings.log_file, encoding="utf-8"))

    redaction_filter = SecretRedactionFilter(
        [
            settings.openai_api_key,
            settings.gemini_api_key,
            settings.max_bot_token,
            settings.robokassa_merchant_login,
            settings.robokassa_password1,
            settings.robokassa_password2,
            settings.robokassa_password3,
        ]
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
    if not settings.max_poll_observe_only and not settings.max_owner_user_ids:
        errors.append("MAX configuration missing: MAX_OWNER_USER_IDS")
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
            ca_bundle=settings.max_ca_bundle_path,
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
    print(json.dumps({"mode": "execute" if execute else "dry-run", "count": len(due)}))
    return 0


def _commerce_user(database: Database, platform_user_id: str) -> sqlite3.Row:
    with database.read() as connection:
        row = connection.execute(
            "SELECT id FROM users WHERE platform='max' AND platform_user_id=?",
            (platform_user_id,),
        ).fetchone()
    if row is None:
        raise ValueError("MAX user was not found")
    return row


def run_credit_report(
    settings: Settings, platform_user_id: str, *, history: bool, output_format: str
) -> int:
    try:
        database = Database(settings.database_path)
        user = _commerce_user(database, platform_user_id)
        balance = CommerceService(database).balance(user["id"])
        result: dict[str, object] = {
            "user_ref": mask_reference(platform_user_id, prefix="max"),
            "available_variants": balance.available,
            "reserved_variants": balance.reserved,
            "total_granted": balance.total_granted,
            "total_consumed": balance.total_consumed,
            "total_refunded": balance.total_refunded,
        }
        if history:
            with database.read() as connection:
                rows = connection.execute(
                    """SELECT delta,event_type,reference_type,reference_id,balance_after,
                              reserved_after,created_at FROM credit_ledger
                       WHERE user_id=? ORDER BY id DESC LIMIT 100""",
                    (user["id"],),
                ).fetchall()
            result["history"] = [
                {
                    "delta": row["delta"],
                    "event": row["event_type"],
                    "reference_type": row["reference_type"],
                    "reference_ref": mask_reference(row["reference_id"]),
                    "available_after": row["balance_after"],
                    "reserved_after": row["reserved_after"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        _print_operator(result, output_format)
        return 0
    except (ValueError, sqlite3.Error) as exc:
        LOGGER.error("Credit report failed: %s", exc)
        return 2


def run_entitlement_report(
    settings: Settings, platform_user_id: str, *, history: bool, output_format: str
) -> int:
    try:
        database = Database(settings.database_path)
        user = _commerce_user(database, platform_user_id)
        balance = CommerceService(database).entitlement_balance(user["id"])
        result: dict[str, object] = {
            "user_ref": mask_reference(platform_user_id, prefix="max"),
            "available_originals": balance.available,
            "consumed_originals": balance.consumed,
            "refunded_originals": balance.refunded,
        }
        if history:
            with database.read() as connection:
                rows = connection.execute(
                    """SELECT status,source_payment_order_id,gallery_version_id,
                              created_at,reserved_at,consumed_at,updated_at
                       FROM unlock_entitlements WHERE user_id=?
                       ORDER BY created_at DESC LIMIT 100""",
                    (user["id"],),
                ).fetchall()
            result["history"] = [
                {
                    "status": row["status"],
                    "order_ref": mask_reference(row["source_payment_order_id"], prefix="ord"),
                    "version_ref": mask_reference(row["gallery_version_id"], prefix="ver"),
                    "created_at": row["created_at"],
                    "consumed_at": row["consumed_at"],
                }
                for row in rows
            ]
        _print_operator(result, output_format)
        return 0
    except (ValueError, sqlite3.Error) as exc:
        LOGGER.error("Entitlement report failed: %s", exc)
        return 2


def run_commerce_adjust(
    settings: Settings,
    args: argparse.Namespace,
    *,
    entity: str,
) -> int:
    try:
        if not args.reason.strip():
            raise ValueError("Non-empty audit reason is required")
        database = Database(settings.database_path)
        user = _commerce_user(database, args.platform_user_id)
        service = CommerceService(database)
        if entity == "generation_credit":
            current = service.balance(user["id"]).available
        else:
            current = service.entitlement_balance(user["id"]).available
        proposed = current + args.delta
        if proposed < 0:
            raise ValueError("Adjustment would make the balance negative")
        if not args.apply:
            _print_operator(
                {
                    "mode": "dry-run",
                    "user_ref": mask_reference(args.platform_user_id, prefix="max"),
                    "entity": entity,
                    "delta": args.delta,
                    "available_before": current,
                    "available_after": proposed,
                    "reason": args.reason,
                    "database_mutated": False,
                },
                args.format,
            )
            return 0
        if not args.idempotency_key:
            raise ValueError("--idempotency-key is required with --apply")
        with database.transaction() as connection:
            if entity == "generation_credit":
                updated = service.adjust_generation_credits(
                    connection,
                    user_id=user["id"],
                    delta=args.delta,
                    reason=args.reason,
                    idempotency_key=args.idempotency_key,
                )
                available = updated.available
            else:
                updated = service.adjust_unlock_entitlements(
                    connection,
                    user_id=user["id"],
                    delta=args.delta,
                    reason=args.reason,
                    idempotency_key=args.idempotency_key,
                )
                available = updated.available
        _print_operator(
            {
                "mode": "apply",
                "user_ref": mask_reference(args.platform_user_id, prefix="max"),
                "entity": entity,
                "delta": args.delta,
                "available_after": available,
                "reason": args.reason,
                "audit_written": True,
            },
            args.format,
        )
        return 0
    except (ValueError, sqlite3.Error, DemoError) as exc:
        LOGGER.error("Commerce adjustment failed: %s", exc)
        return 2


def run_package_status(
    settings: Settings,
    platform_user_id: str | None,
    invoice: int | None,
    output_format: str,
) -> int:
    try:
        database = Database(settings.database_path)
        clauses = ["o.product_code=?"]
        values: list[object] = [PRODUCT_CODE]
        user_ref = None
        if platform_user_id:
            user = _commerce_user(database, platform_user_id)
            clauses.append("o.user_id=?")
            values.append(user["id"])
            user_ref = mask_reference(platform_user_id, prefix="max")
        if invoice:
            clauses.append("o.provider_invoice_id=?")
            values.append(invoice)
        with database.read() as connection:
            rows = connection.execute(
                f"""SELECT o.provider_invoice_id,o.status,o.amount_minor,o.created_at,
                            g.status AS grant_status,l.available_credits,l.reserved_credits,
                            l.consumed_credits,e.status AS entitlement_status
                     FROM payment_orders o
                     LEFT JOIN continuation_pack_grants g ON g.payment_order_id=o.id
                     LEFT JOIN generation_credit_lots l ON l.id=g.credit_lot_id
                     LEFT JOIN unlock_entitlements e ON e.id=g.entitlement_id
                     WHERE {' AND '.join(clauses)} ORDER BY o.created_at DESC LIMIT 100""",
                values,
            ).fetchall()
        _print_operator(
            {
                "product_code": PRODUCT_CODE,
                "user_ref": user_ref,
                "packages": [
                    {
                        "invoice_ref": mask_reference(row["provider_invoice_id"], prefix="inv"),
                        "payment_status": row["status"],
                        "grant_status": row["grant_status"],
                        "price_rub": row["amount_minor"] / 100,
                        "variants_available": row["available_credits"],
                        "variants_reserved": row["reserved_credits"],
                        "variants_consumed": row["consumed_credits"],
                        "original_status": row["entitlement_status"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ],
            },
            output_format,
        )
        return 0
    except (ValueError, sqlite3.Error) as exc:
        LOGGER.error("Package status failed: %s", exc)
        return 2


def _backup_manager(settings: Settings) -> BackupManager:
    return BackupManager(
        settings.database_path,
        settings.backup_dir_path,
        settings.backup_retention_days,
    )


def _passphrase_from_stdin(enabled: bool) -> str:
    if not enabled:
        raise BackupError("Passphrase input must use --passphrase-stdin")
    return sys.stdin.readline().rstrip("\r\n")


def run_backup_create(settings: Settings, passphrase_stdin: bool) -> int:
    try:
        report = _backup_manager(settings).create(
            _passphrase_from_stdin(passphrase_stdin)
        )
    except BackupError as exc:
        LOGGER.error("Backup creation failed: %s", exc)
        return 1
    except (OSError, sqlite3.Error) as exc:
        LOGGER.error("Backup creation failed safely (error_type=%s)", type(exc).__name__)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


def run_backup_restore_test(
    settings: Settings, backup: str, passphrase_stdin: bool
) -> int:
    try:
        report = _backup_manager(settings).restore_test(
            Path(backup), _passphrase_from_stdin(passphrase_stdin)
        )
    except BackupError as exc:
        LOGGER.error("Backup restore test failed: %s", exc)
        return 1
    except (OSError, sqlite3.Error) as exc:
        LOGGER.error("Backup restore test failed safely (error_type=%s)", type(exc).__name__)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


def run_backup_mark_offsite(settings: Settings, backup: str, provider: str) -> int:
    try:
        report = _backup_manager(settings).mark_offsite(backup, provider)
    except BackupError as exc:
        LOGGER.error("Off-site backup mark failed: %s", exc)
        return 1
    except OSError as exc:
        LOGGER.error("Off-site backup mark failed safely (error_type=%s)", type(exc).__name__)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


def run_maintenance_command(settings: Settings, execute: bool) -> int:
    try:
        report = run_maintenance(settings, execute=execute)
    except (OSError, sqlite3.Error) as exc:
        LOGGER.error("Maintenance failed safely (error_type=%s)", type(exc).__name__)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


def run_provider_context_cleanup(settings: Settings, execute: bool) -> int:
    try:
        database = Database(settings.database_path)
        gateway = (
            OpenAIProviderContextGateway(create_openai_client(settings))
            if settings.openai_api_key
            else None
        )
        service = ProviderContextService(settings, database, gateway)
        due = service.cleanup_due(execute=execute)
        with database.read() as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM provider_contexts WHERE status='delete_pending'"
            ).fetchone()[0]
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        LOGGER.error("Provider context cleanup failed safely (error_type=%s)", type(exc).__name__)
        return 1
    print(json.dumps({
        "mode": "execute" if execute else "dry-run",
        "due_count": len(due),
        "pending_count": pending,
    }, sort_keys=True))
    return 0


def run_ai_inspect(settings: Settings, attempt_id: str) -> int:
    """Print provider intent without platform identity, paths or credentials."""

    database = Database(settings.database_path)
    with database.read() as connection:
        row = connection.execute(
            "SELECT * FROM generation_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    if row is None:
        LOGGER.error("Generation attempt was not found")
        return 2
    plan = (
        EditPlan.from_json(row["edit_plan_json"])
        if row["edit_plan_json"]
        else EditPlan.from_legacy(
            row["correction_prompt"] or row["prompt"],
            correction=bool(row["correction"]),
            correction_target_version_id=row["parent_version_id"],
        )
    )
    view = safe_prompt_inspection(
        plan, row["provider_prompt"] or row["effective_prompt"] or row["prompt"]
    )
    view.update({
        "provider": row["provider"],
        "model": row["model"],
        "requested_quality": row["requested_quality"],
        "requested_size": row["requested_size"],
        "prompt_builder_version": (
            row["prompt_builder_version"] or view["prompt_builder_version"]
        ),
        "uses_parent_image": bool(row["source_version_id"]),
        "status": row["status"],
    })
    print(json.dumps(view, ensure_ascii=False, indent=2))
    return 0


def run_status_report(
    settings: Settings,
    command: str,
    *,
    online: bool = False,
    output_format: str = "json",
) -> int:
    database = Database(settings.database_path)
    collectors = {
        "pilot-status": lambda: pilot_status(settings, database),
        "pilot-report": lambda: pilot_report(settings, database),
        "payment-status": lambda: payment_status(settings, database),
        "robokassa-health": lambda: robokassa_health(settings, database),
        "storage-status": lambda: storage_status(settings, database),
        "backup-status": lambda: backup_status(settings),
        "cleanup-status": lambda: cleanup_status(settings),
        "cost-status": lambda: cost_status(settings, database),
        "openai-budget-status": lambda: openai_budget_status(settings),
        "monitoring-status": lambda: collect_launch_status(
            settings, online=False
        )["monitoring"],
        "health-report": lambda: health_report(settings, online=online),
    }
    _print_operator(collectors[command](), output_format)
    return 0


def _refund_values(args: argparse.Namespace) -> tuple[int, str]:
    amount = Decimal(args.amount_rub).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise ValueError("Refund amount must be positive")
    return int(amount * 100), str(args.reason)


def run_refund_prepare(settings: Settings, args: argparse.Namespace) -> int:
    try:
        amount_minor, reason = _refund_values(args)
        service = build_payment_service(settings, Database(settings.database_path))
        preview = service.preview_refund(
            args.order_id, amount_minor, reason, args.idempotency_key
        )
        if not args.apply:
            _print_operator({
                "mode": "dry-run",
                "order_ref": mask_reference(preview.order_id, prefix="ord"),
                "amount_rub": preview.amount_minor / 100,
                "available_rub": preview.available_minor / 100,
                "reason": preview.reason,
                "idempotent_existing": preview.idempotent_existing,
                "provider_called": False,
                "database_mutated": False,
            }, args.format)
            return 0
        refund = service.prepare_refund(
            args.order_id, amount_minor, reason, args.idempotency_key
        )
    except (InvalidOperation, ValueError, PaymentError) as exc:
        LOGGER.error("Refund preparation rejected: %s", exc)
        return 2
    _print_operator({
        "mode": "apply",
        "refund_ref": mask_reference(refund.id, prefix="ref"),
        "order_ref": mask_reference(refund.order_id, prefix="ord"),
        "amount_rub": refund.amount_minor / 100,
        "status": refund.status.value,
        "provider_called": False,
        "audit_event": "refund_prepared",
    }, args.format)
    return 0


def run_refund_submit(
    settings: Settings, refund_id: str, *, apply: bool, output_format: str
) -> int:
    if not apply:
        _print_operator({
            "mode": "dry-run",
            "refund_ref": mask_reference(refund_id, prefix="ref"),
            "provider_called": False,
            "database_mutated": False,
            "next_step": "Repeat with --apply only after operator verification.",
        }, output_format)
        return 0
    try:
        refund = build_payment_service(settings, Database(settings.database_path)).submit_refund(
            refund_id
        )
    except PaymentUnavailable as exc:
        LOGGER.error("Refund execution is disabled: %s", exc)
        return 3
    except PaymentError as exc:
        LOGGER.error("Refund submission rejected: %s", exc)
        return 2
    _print_operator({
        "mode": "apply",
        "refund_ref": mask_reference(refund.id, prefix="ref"),
        "status": refund.status.value,
        "provider_request_created": bool(refund.provider_request_id),
    }, output_format)
    return 0


def run_refund_status(
    settings: Settings, refund_id: str, refresh: bool, output_format: str
) -> int:
    service = build_payment_service(settings, Database(settings.database_path))
    try:
        if refresh:
            refund = service.refresh_refund(refund_id)
        else:
            with service.database.read() as connection:
                row = connection.execute(
                    "SELECT * FROM refund_intents WHERE id=?", (refund_id,)
                ).fetchone()
            if row is None:
                raise PaymentError("Refund was not found")
            output = {
                "refund_ref": mask_reference(row["id"], prefix="ref"),
                "order_ref": mask_reference(row["order_id"], prefix="ord"),
                "amount_rub": int(row["amount_minor"]) / 100,
                "status": str(row["status"]),
                "provider_request_created": bool(row["provider_request_id"]),
                "provider_refreshed": False,
            }
            _print_operator(output, output_format)
            return 0
    except PaymentUnavailable as exc:
        LOGGER.error("Refund status refresh is disabled: %s", exc)
        return 3
    except PaymentError as exc:
        LOGGER.error("Refund status unavailable: %s", exc)
        return 2
    _print_operator({
        "refund_ref": mask_reference(refund.id, prefix="ref"),
        "order_ref": mask_reference(refund.order_id, prefix="ord"),
        "amount_rub": refund.amount_minor / 100,
        "status": refund.status.value,
        "provider_request_created": bool(refund.provider_request_id),
        "provider_refreshed": True,
    }, output_format)
    return 0


def run_payment_history(
    settings: Settings,
    order_id: str | None,
    invoice: int | None,
    output_format: str,
) -> int:
    database = Database(settings.database_path)
    if order_id is None and invoice is None:
        try:
            _print_operator(payment_reconcile(database), output_format)
            return 0
        except PaymentError as exc:
            LOGGER.error("Payment history unavailable: %s", exc)
            return 2
    service = build_payment_service(settings, database)
    try:
        resolved_order_id = (
            service.order_by_invoice(invoice).id if invoice is not None else str(order_id)
        )
        history = service.history(resolved_order_id)
    except PaymentError as exc:
        LOGGER.error("Payment history unavailable: %s", exc)
        return 2
    _print_operator({
        "invoice_ref": mask_reference(history.order.provider_invoice_id, prefix="inv"),
        "order_ref": mask_reference(history.order.id, prefix="ord"),
        "version_ref": mask_reference(history.order.version_id, prefix="ver"),
        "status": history.order.status.value,
        "amount_rub": history.order.amount_minor / 100,
        "currency": history.order.currency,
        "events": [entry.__dict__ for entry in history.audit],
    }, output_format)
    return 0


def run_refund_history(
    settings: Settings, refund_id: str | None, output_format: str
) -> int:
    database = Database(settings.database_path)
    if refund_id is None:
        with database.read() as connection:
            rows = connection.execute(
                """SELECT r.id,r.order_id,r.amount_minor,r.currency,r.reason,r.status,r.created_at
                   FROM refund_intents r ORDER BY r.created_at DESC LIMIT 50"""
            ).fetchall()
        _print_operator({
            "count": len(rows),
            "refunds": [
                {
                    "refund_ref": mask_reference(row["id"], prefix="ref"),
                    "order_ref": mask_reference(row["order_id"], prefix="ord"),
                    "amount_rub": int(row["amount_minor"]) / 100,
                    "currency": str(row["currency"]),
                    "reason": str(row["reason"]),
                    "status": str(row["status"]),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ],
        }, output_format)
        return 0
    try:
        history = build_payment_service(
            settings, database
        ).refund_history(refund_id)
    except PaymentError as exc:
        LOGGER.error("Refund history unavailable: %s", exc)
        return 2
    _print_operator({
        "refund_ref": mask_reference(history.refund.id, prefix="ref"),
        "order_ref": mask_reference(history.refund.order_id, prefix="ord"),
        "amount_rub": history.refund.amount_minor / 100,
        "currency": history.refund.currency,
        "reason": history.refund.reason,
        "status": history.refund.status.value,
        "events": [entry.__dict__ for entry in history.audit],
    }, output_format)
    return 0


def run_payment_show(settings: Settings, invoice: int, output_format: str) -> int:
    try:
        report = payment_show(Database(settings.database_path), invoice)
    except PaymentError as exc:
        LOGGER.error("Payment order unavailable: %s", exc)
        return 2
    _print_operator(report, output_format)
    return 0 if report["consistent"] else 4


def run_payment_reconcile(
    settings: Settings, invoice: int | None, output_format: str
) -> int:
    try:
        report = payment_reconcile(Database(settings.database_path), invoice)
    except PaymentError as exc:
        LOGGER.error("Payment reconciliation failed: %s", exc)
        return 2
    _print_operator(report, output_format)
    return 0 if report["mismatch_count"] == 0 else 4


def run_payment_expiration_reconcile(
    settings: Settings, *, apply: bool, output_format: str
) -> int:
    report = payment_expiration_reconcile(
        Database(settings.database_path), apply=apply
    )
    _print_operator(report, output_format)
    return 0


def run_delivery_retry(
    settings: Settings,
    invoice: int,
    *,
    apply: bool,
    output_format: str,
) -> int:
    service = build_payment_service(settings, Database(settings.database_path))
    try:
        order = service.order_by_invoice(invoice)
        if order.status.value != "delivery_pending":
            raise PaymentError("Only a delivery-pending order can be scheduled for retry")
        if not apply:
            _print_operator({
                "mode": "dry-run",
                "invoice_ref": mask_reference(invoice, prefix="inv"),
                "order_ref": mask_reference(order.id, prefix="ord"),
                "status": order.status.value,
                "database_mutated": False,
                "audit_event": None,
            }, output_format)
            return 0
        service.schedule_delivery_retry(order.id)
    except PaymentError as exc:
        LOGGER.error("Delivery retry scheduling rejected: %s", exc)
        return 2
    _print_operator({
        "mode": "apply",
        "invoice_ref": mask_reference(invoice, prefix="inv"),
        "order_ref": mask_reference(order.id, prefix="ord"),
        "status": order.status.value,
        "audit_event": "delivery_retry_scheduled",
    }, output_format)
    return 0


def run_resend_original(
    settings: Settings,
    invoice: int,
    *,
    apply: bool,
    output_format: str,
) -> int:
    database = Database(settings.database_path)
    service = build_payment_service(settings, database)
    try:
        order = service.order_by_invoice(invoice)
        service.original_for_order(order.id, order.user_id)
        if not apply:
            _print_operator({
                "mode": "dry-run",
                "invoice_ref": mask_reference(invoice, prefix="inv"),
                "order_ref": mask_reference(order.id, prefix="ord"),
                "status": order.status.value,
                "original_available": True,
                "max_called": False,
                "database_mutated": False,
            }, output_format)
            return 0
        from app.max_runtime import build_max_application

        application, client, _store = build_max_application(settings)
        try:
            delivered = application.deliver_paid_original(order.id)
        finally:
            client.close()
    except (PaymentError, OSError) as exc:
        LOGGER.error("Original redelivery rejected: %s", exc)
        return 2
    _print_operator({
        "mode": "apply",
        "invoice_ref": mask_reference(invoice, prefix="inv"),
        "order_ref": mask_reference(order.id, prefix="ord"),
        "delivered": bool(delivered),
        "audit_event": "original_delivered" if delivered else "original_delivery_failed",
    }, output_format)
    return 0 if delivered else 5


def run_refund_create(settings: Settings, args: argparse.Namespace) -> int:
    try:
        amount_minor, reason = _refund_values(args)
        service = build_payment_service(settings, Database(settings.database_path))
        order = service.order_by_invoice(args.invoice)
        preview = service.preview_refund(
            order.id, amount_minor, reason, args.idempotency_key
        )
        if not args.apply:
            _print_operator({
                "mode": "dry-run",
                "invoice_ref": mask_reference(args.invoice, prefix="inv"),
                "order_ref": mask_reference(order.id, prefix="ord"),
                "amount_rub": preview.amount_minor / 100,
                "available_rub": preview.available_minor / 100,
                "reason": preview.reason,
                "provider_called": False,
                "database_mutated": False,
            }, args.format)
            return 0
        refund = service.prepare_refund(
            order.id, amount_minor, reason, args.idempotency_key
        )
    except (InvalidOperation, ValueError, PaymentError) as exc:
        LOGGER.error("Refund creation rejected: %s", exc)
        return 2
    _print_operator({
        "mode": "apply",
        "invoice_ref": mask_reference(args.invoice, prefix="inv"),
        "refund_ref": mask_reference(refund.id, prefix="ref"),
        "amount_rub": refund.amount_minor / 100,
        "status": refund.status.value,
        "provider_called": False,
        "audit_event": "refund_prepared",
    }, args.format)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photo-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_format(target: argparse.ArgumentParser) -> None:
        target.add_argument("--format", choices=("json", "human"), default="json")

    for command in ("health", "openai-check", "max-check", "run", "demo-stats"):
        subparsers.add_parser(command)
    cleanup = subparsers.add_parser("gallery-cleanup")
    cleanup.add_argument("--execute", action="store_true")
    maintenance = subparsers.add_parser("maintenance-cleanup")
    maintenance.add_argument("--execute", action="store_true")
    provider_cleanup = subparsers.add_parser("provider-context-cleanup")
    provider_cleanup.add_argument("--execute", action="store_true")
    backup_create = subparsers.add_parser("backup-create")
    backup_create.add_argument("--passphrase-stdin", action="store_true")
    backup_restore = subparsers.add_parser("backup-restore-test")
    backup_restore.add_argument("--backup", required=True)
    backup_restore.add_argument("--passphrase-stdin", action="store_true")
    backup_offsite = subparsers.add_parser("backup-mark-offsite")
    backup_offsite.add_argument("--backup", required=True)
    backup_offsite.add_argument("--provider", required=True)
    launch_status = subparsers.add_parser("launch-status")
    launch_status.add_argument("--strict", action="store_true")
    for command in (
        "pilot-status", "pilot-report", "payment-status", "robokassa-health",
        "storage-status", "backup-status", "cleanup-status", "cost-status",
        "openai-budget-status", "monitoring-status",
    ):
        add_format(subparsers.add_parser(command))
    health_report_parser = subparsers.add_parser("health-report")
    health_report_parser.add_argument("--online", action="store_true")
    add_format(health_report_parser)
    for command in ("credit-status", "credit-history", "entitlement-status", "entitlement-history"):
        report_parser = subparsers.add_parser(command)
        report_parser.add_argument("--platform-user-id", required=True)
        add_format(report_parser)
    for command in ("credit-adjust", "entitlement-adjust"):
        adjust_parser = subparsers.add_parser(command)
        adjust_parser.add_argument("--platform-user-id", required=True)
        adjust_parser.add_argument("--delta", required=True, type=int)
        adjust_parser.add_argument("--reason", required=True)
        adjust_parser.add_argument("--idempotency-key")
        adjust_parser.add_argument("--apply", action="store_true")
        add_format(adjust_parser)
    package_parser = subparsers.add_parser("package-status")
    package_parser.add_argument("--platform-user-id")
    package_parser.add_argument("--invoice", type=int)
    add_format(package_parser)
    refund_prepare = subparsers.add_parser("refund-prepare")
    refund_prepare.add_argument("--order-id", required=True)
    refund_prepare.add_argument("--amount-rub", required=True)
    refund_prepare.add_argument(
        "--reason", required=True, choices=tuple(reason.value for reason in RefundReason)
    )
    refund_prepare.add_argument("--idempotency-key", required=True)
    refund_prepare.add_argument("--apply", action="store_true")
    add_format(refund_prepare)
    refund_submit = subparsers.add_parser("refund-submit")
    refund_submit.add_argument("--refund-id", required=True)
    refund_submit.add_argument("--apply", action="store_true")
    add_format(refund_submit)
    refund_status_parser = subparsers.add_parser("refund-status")
    refund_status_parser.add_argument("--refund-id", required=True)
    refund_status_parser.add_argument("--refresh", action="store_true")
    add_format(refund_status_parser)
    payment_history_parser = subparsers.add_parser("payment-history")
    payment_history_selector = payment_history_parser.add_mutually_exclusive_group()
    payment_history_selector.add_argument("--order-id")
    payment_history_selector.add_argument("--invoice", type=int)
    add_format(payment_history_parser)
    payment_show_parser = subparsers.add_parser("payment-show")
    payment_show_parser.add_argument("--invoice", required=True, type=int)
    add_format(payment_show_parser)
    payment_reconcile_parser = subparsers.add_parser("payment-reconcile")
    payment_reconcile_parser.add_argument("--invoice", type=int)
    add_format(payment_reconcile_parser)
    expiration_reconcile_parser = subparsers.add_parser(
        "payment-expiration-reconcile"
    )
    expiration_reconcile_parser.add_argument("--apply", action="store_true")
    add_format(expiration_reconcile_parser)
    resend_parser = subparsers.add_parser("payment-resend-original")
    resend_parser.add_argument("--invoice", required=True, type=int)
    resend_parser.add_argument("--apply", action="store_true")
    add_format(resend_parser)
    retry_parser = subparsers.add_parser("payment-mark-delivery-retry")
    retry_parser.add_argument("--invoice", required=True, type=int)
    retry_parser.add_argument("--apply", action="store_true")
    add_format(retry_parser)
    refund_history_parser = subparsers.add_parser("refund-history")
    refund_history_parser.add_argument("--refund-id")
    add_format(refund_history_parser)
    refund_create = subparsers.add_parser("refund-create")
    refund_create.add_argument("--invoice", required=True, type=int)
    refund_create.add_argument("--amount-rub", required=True)
    refund_create.add_argument(
        "--reason", required=True, choices=tuple(reason.value for reason in RefundReason)
    )
    refund_create.add_argument("--idempotency-key", required=True)
    refund_create.add_argument("--dry-run", action="store_true")
    refund_create.add_argument("--apply", action="store_true")
    add_format(refund_create)
    demo = subparsers.add_parser("demo-edit")
    demo.add_argument("--user-id", required=True)
    demo.add_argument("--image", required=True)
    demo.add_argument("--prompt", required=True)
    demo.add_argument("--scenario")
    demo.add_argument("--event-id")
    demo.add_argument(
        "--provider",
        choices=("openai", "gemini", "nanobanana", "fake"),
        default=None,
        help="override IMAGE_PROVIDER for this local command",
    )
    inspect = subparsers.add_parser("ai-inspect")
    inspect.add_argument("--attempt-id", required=True)
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
    if args.command == "maintenance-cleanup":
        return run_maintenance_command(settings, args.execute)
    if args.command == "provider-context-cleanup":
        return run_provider_context_cleanup(settings, args.execute)
    if args.command == "backup-create":
        return run_backup_create(settings, args.passphrase_stdin)
    if args.command == "backup-restore-test":
        return run_backup_restore_test(settings, args.backup, args.passphrase_stdin)
    if args.command == "backup-mark-offsite":
        return run_backup_mark_offsite(settings, args.backup, args.provider)
    if args.command == "launch-status":
        return print_launch_status(settings, strict=args.strict)
    if args.command in {
        "pilot-status", "pilot-report", "payment-status", "robokassa-health",
        "storage-status", "backup-status", "cleanup-status", "cost-status",
        "openai-budget-status", "monitoring-status", "health-report",
    }:
        return run_status_report(
            settings,
            args.command,
            online=getattr(args, "online", False),
            output_format=args.format,
        )
    if args.command == "refund-prepare":
        return run_refund_prepare(settings, args)
    if args.command in {"credit-status", "credit-history"}:
        return run_credit_report(
            settings,
            args.platform_user_id,
            history=args.command == "credit-history",
            output_format=args.format,
        )
    if args.command in {"entitlement-status", "entitlement-history"}:
        return run_entitlement_report(
            settings,
            args.platform_user_id,
            history=args.command == "entitlement-history",
            output_format=args.format,
        )
    if args.command in {"credit-adjust", "entitlement-adjust"}:
        return run_commerce_adjust(
            settings,
            args,
            entity=(
                "generation_credit" if args.command == "credit-adjust"
                else "unlock_entitlement"
            ),
        )
    if args.command == "package-status":
        return run_package_status(
            settings, args.platform_user_id, args.invoice, args.format
        )
    if args.command == "refund-submit":
        return run_refund_submit(
            settings, args.refund_id, apply=args.apply, output_format=args.format
        )
    if args.command == "refund-status":
        return run_refund_status(
            settings, args.refund_id, args.refresh, args.format
        )
    if args.command == "payment-history":
        return run_payment_history(
            settings, args.order_id, args.invoice, args.format
        )
    if args.command == "payment-show":
        return run_payment_show(settings, args.invoice, args.format)
    if args.command == "payment-reconcile":
        return run_payment_reconcile(settings, args.invoice, args.format)
    if args.command == "payment-expiration-reconcile":
        return run_payment_expiration_reconcile(
            settings, apply=args.apply, output_format=args.format
        )
    if args.command == "payment-resend-original":
        return run_resend_original(
            settings, args.invoice, apply=args.apply, output_format=args.format
        )
    if args.command == "payment-mark-delivery-retry":
        return run_delivery_retry(
            settings, args.invoice, apply=args.apply, output_format=args.format
        )
    if args.command == "refund-history":
        return run_refund_history(settings, args.refund_id, args.format)
    if args.command == "refund-create":
        if args.dry_run and args.apply:
            LOGGER.error("--dry-run and --apply are mutually exclusive")
            return 2
        return run_refund_create(settings, args)
    if args.command == "ai-inspect":
        return run_ai_inspect(settings, args.attempt_id)
    return run_process(settings)


if __name__ == "__main__":
    raise SystemExit(main())
