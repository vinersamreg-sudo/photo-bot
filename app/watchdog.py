"""Privacy-safe production watchdog built on existing Ravuna health signals."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.operations import collect_launch_status
from app.payment_admin import payment_reconciliation_summary
from app.database import ReadOnlyDatabase


EXPECTED_SCHEMA_VERSION = 12
UNKNOWN_PAYMENT_TOKEN = "0" * 32


@dataclass(frozen=True)
class WatchdogCheck:
    name: str
    state: str
    detail: str


class AlertTransport(Protocol):
    configured: bool

    def send(self, report: dict[str, Any]) -> None: ...


class NullAlertTransport:
    configured = False

    def send(self, report: dict[str, Any]) -> None:
        del report


class WebhookAlertTransport:
    configured = True

    def __init__(self, destination: str) -> None:
        parsed = urlsplit(destination)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Alert webhook destination must use HTTPS")
        self._destination = destination

    def send(self, report: dict[str, Any]) -> None:
        payload = {
            "source": "ravuna-production-watchdog",
            "status": report["status"],
            "checked_at": report["checked_at"],
            "attention": [
                check["name"]
                for check in report["checks"]
                if check["state"] != "PASS"
            ],
        }
        response = httpx.post(self._destination, json=payload, timeout=10)
        response.raise_for_status()


def read_only_application_errors(settings: Settings) -> list[str]:
    """Check runtime prerequisites without creating probe files."""

    errors: list[str] = []
    if sys.version_info[:3] < (3, 12, 0):
        errors.append("python_version")
    for name, path in (
        ("data", settings.data_dir),
        ("logs", settings.logs_dir),
        ("temp", settings.temp_dir),
    ):
        if not path.is_dir():
            errors.append(f"{name}_missing")
        elif not os.access(path, os.R_OK | os.W_OK):
            errors.append(f"{name}_permissions")
    return errors


def alert_transport_from_environment(values: dict[str, str]) -> AlertTransport:
    destination = values.get("RAVUNA_ALERT_WEBHOOK_URL", "").strip()
    return WebhookAlertTransport(destination) if destination else NullAlertTransport()


def probe_payment_routes(settings: Settings) -> dict[str, int | None]:
    """Probe only public route shape; never creates a payment."""

    paths = {
        "short": f"https://ravuna.ru/p/{UNKNOWN_PAYMENT_TOKEN}",
        "success": f"https://ravuna.ru/payment/success/{UNKNOWN_PAYMENT_TOKEN}",
        "fail": f"https://ravuna.ru/payment/fail/{UNKNOWN_PAYMENT_TOKEN}",
        "result": settings.payment_result_url,
    }
    statuses: dict[str, int | None] = {name: None for name in paths}
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            for name, url in paths.items():
                if not url.startswith("https://"):
                    continue
                try:
                    statuses[name] = client.get(url).status_code
                except httpx.HTTPError:
                    statuses[name] = None
    except (OSError, httpx.HTTPError):
        pass
    return statuses


def _recovery_status(settings: Settings) -> dict[str, Any]:
    def read(name: str) -> dict[str, Any] | None:
        try:
            value = json.loads((settings.backup_dir_path / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    latest = read("latest_recovery.json")
    restored = read("recovery_restore_status.json")
    offsite = read("recovery_offsite_status.json")
    age_hours: float | None = None
    if latest:
        try:
            created = datetime.fromisoformat(str(latest["created_at"]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_hours = max(
                0.0,
                (datetime.now(timezone.utc) - created).total_seconds() / 3600,
            )
        except (KeyError, TypeError, ValueError):
            pass
    name = latest.get("recovery_bundle_name") if latest else None
    return {
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "restore_tested": bool(
            name
            and restored
            and restored.get("backup_name") == name
            and restored.get("overall") == "PASS"
        ),
        "offsite_copied": bool(
            name and offsite and offsite.get("backup_name") == name
        ),
    }


def build_watchdog_report(
    settings: Settings,
    *,
    online: bool,
    application_errors: list[str] | None = None,
    launch: dict[str, Any] | None = None,
    payment_routes: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    """Evaluate operational signals without emitting identifiers or content."""

    launch = launch or collect_launch_status(settings, online=online)
    reconciliation = payment_reconciliation_summary(
        ReadOnlyDatabase(settings.database_path)
    )
    routes = payment_routes if payment_routes is not None else (
        probe_payment_routes(settings) if online else {}
    )
    application_errors = application_errors or []
    checks: list[WatchdogCheck] = []

    def add(name: str, state: str, detail: str) -> None:
        checks.append(WatchdogCheck(name, state, detail))

    runtime = launch["runtime"]
    database = launch["database"]
    processing = launch["processing"]
    cleanup = launch["cleanup"]
    backup = launch["backup"]
    provider = launch["provider"]
    max_status = launch["max"]
    site = launch["site"]
    recovery = _recovery_status(settings)

    add(
        "service",
        "PASS" if runtime["service_active"] else "FAIL",
        "active" if runtime["service_active"] else "inactive",
    )
    add(
        "application_health",
        "PASS" if not application_errors else "FAIL",
        "OK" if not application_errors else f"errors={len(application_errors)}",
    )
    poll_age = max_status.get("last_poll_age_hours")
    poll_fresh = bool(
        poll_age is not None
        and float(poll_age) * 3600 <= settings.max_poll_max_stale_seconds
        and max_status.get("connected") is not False
    )
    add(
        "max_polling",
        "PASS" if poll_fresh else "FAIL",
        f"age_seconds={round(float(poll_age) * 3600) if poll_age is not None else 'missing'}",
    )
    add(
        "sqlite",
        "PASS" if database.get("quick_check") == "ok" else "FAIL",
        str(database.get("quick_check", "unavailable")),
    )
    migration = int(database.get("migration") or 0)
    add(
        "migration",
        "PASS" if migration == EXPECTED_SCHEMA_VERSION else "FAIL",
        f"version={migration},expected={EXPECTED_SCHEMA_VERSION}",
    )
    active = int(processing.get("active_attempts") or 0) + int(
        processing.get("active_dialogs") or 0
    )
    orphans = cleanup.get("remaining_orphan_count")
    add(
        "processing_storage",
        "PASS" if orphans == 0 else "WARN",
        f"active={active},orphans={orphans if orphans is not None else 'unknown'}",
    )
    free_mb = int(launch["storage"].get("free_mb") or 0)
    minimum_mb = int(launch["storage"].get("minimum_free_mb") or 0)
    add(
        "disk",
        "PASS" if free_mb >= minimum_mb else "FAIL",
        f"free_mb={free_mb},minimum_mb={minimum_mb}",
    )
    sqlite_backup_ok = bool(
        backup.get("latest_age_hours") is not None
        and float(backup["latest_age_hours"]) <= settings.backup_max_age_hours
        and backup.get("restore_tested")
        and backup.get("offsite_copied")
    )
    recovery_ok = bool(
        recovery["age_hours"] is not None
        and float(recovery["age_hours"]) <= settings.backup_max_age_hours
        and recovery["restore_tested"]
        and recovery["offsite_copied"]
    )
    add(
        "backup_recovery",
        "PASS" if sqlite_backup_ok and recovery_ok else "WARN",
        f"sqlite={'ok' if sqlite_backup_ok else 'attention'},recovery={'ok' if recovery_ok else 'attention'}",
    )
    payment_state = str(reconciliation["status"])
    add(
        "payments",
        "PASS" if payment_state == "OK" else "FAIL" if payment_state == "CRITICAL" else "WARN",
        f"reconciliation={payment_state}",
    )
    provider_configuration_ok = bool(
        provider.get("configured")
        and provider.get("name")
        and provider.get("model")
    )
    add(
        "provider_configuration",
        "PASS" if provider_configuration_ok else "FAIL",
        (
            f"provider={provider.get('name') or 'missing'},"
            f"model={provider.get('model') or 'missing'},"
            f"credentials={'present' if provider.get('configured') else 'missing'}"
        ),
    )
    if online:
        add(
            "website",
            "PASS" if site.get("required_routes_ok") else "FAIL",
            "reachable" if site.get("required_routes_ok") else "unavailable",
        )
        routes_ok = bool(
            routes.get("short") == 404
            and routes.get("success") == 404
            and routes.get("fail") == 404
            and routes.get("result") == 405
        )
        add(
            "payment_routes",
            "PASS" if routes_ok else "FAIL",
            "reachable" if routes_ok else "unexpected_status",
        )
    else:
        add("website", "WARN", "not_checked")
        add("payment_routes", "WARN", "not_checked")

    if any(check.state == "FAIL" for check in checks):
        status = "CRITICAL"
        exit_code = 2
    elif any(check.state == "WARN" for check in checks):
        status = "WARNING"
        exit_code = 1
    else:
        status = "HEALTHY"
        exit_code = 0
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "exit_code": exit_code,
        "checks": [check.__dict__ for check in checks],
        "alert_transport": "not_configured",
    }


def render_watchdog(report: dict[str, Any]) -> str:
    lines = [f"STATUS={report['status']}"]
    lines.extend(
        f"{check['name']}={check['state']} {check['detail']}"
        for check in report["checks"]
    )
    lines.append(f"alert_transport={report['alert_transport']}")
    return "\n".join(lines)
