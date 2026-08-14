"""Privacy-safe production watchdog built on existing Ravuna health signals."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.operations import collect_launch_status
from app.payment_admin import payment_reconciliation_summary
from app.database import ReadOnlyDatabase


EXPECTED_SCHEMA_VERSION = 13
UNKNOWN_PAYMENT_TOKEN = "0" * 32
AUTO_HEAL_COOLDOWN = timedelta(minutes=30)
AUTO_HEAL_READINESS_ATTEMPTS = 6
AUTO_HEAL_READINESS_DELAY_SECONDS = 5
HEALABLE_APPLICATION_ERRORS = frozenset({
    "runtime_process_unreadable",
    "runtime_process_unexpected",
})


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
    if settings.app_env == "production":
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    "photo-bot.service",
                    "--property=MainPID",
                    "--value",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            pid = int(result.stdout.strip() or "0") if result.returncode == 0 else 0
        except (OSError, subprocess.SubprocessError, ValueError):
            pid = 0
        if pid > 0:
            try:
                command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            except OSError:
                errors.append("runtime_process_unreadable")
            else:
                if b"-m app.main run" not in command:
                    errors.append("runtime_process_unexpected")
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


def probe_robokassa(settings: Settings) -> bool:
    """Check provider reachability without creating an order or payment."""

    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get(settings.robokassa_payment_url)
        return response.status_code < 500
    except (OSError, httpx.HTTPError):
        return False


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


def _recent_payment_preparation_failures(
    database: ReadOnlyDatabase,
    *,
    now: datetime | None = None,
) -> int | None:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=30)
    try:
        with database.read() as connection:
            row = connection.execute(
                """SELECT COUNT(*) FROM product_events
                   WHERE event_type='payment_preparation_failed' AND created_at>=?""",
                (cutoff.isoformat(),),
            ).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else 0


def build_watchdog_report(
    settings: Settings,
    *,
    online: bool,
    application_errors: list[str] | None = None,
    launch: dict[str, Any] | None = None,
    payment_routes: dict[str, int | None] | None = None,
    robokassa_available: bool | None = None,
) -> dict[str, Any]:
    """Evaluate operational signals without emitting identifiers or content."""

    launch = launch or collect_launch_status(settings, online=online)
    read_only_database = ReadOnlyDatabase(settings.database_path)
    reconciliation = payment_reconciliation_summary(read_only_database)
    recent_payment_failures = _recent_payment_preparation_failures(read_only_database)
    routes = payment_routes if payment_routes is not None else (
        probe_payment_routes(settings) if online else {}
    )
    if online and robokassa_available is None:
        robokassa_available = probe_robokassa(settings)
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
    max_api_available = max_status.get("connected") if online else None
    add(
        "max_api",
        "PASS" if max_api_available is True else "FAIL" if online else "WARN",
        "reachable" if max_api_available is True else "unavailable" if online else "not_checked",
    )
    add(
        "robokassa_external",
        "PASS" if robokassa_available is True else "FAIL" if online else "WARN",
        "reachable" if robokassa_available is True else "unavailable" if online else "not_checked",
    )
    external_network_ok = bool(
        max_api_available is True
        or robokassa_available is True
        or site.get("required_routes_ok") is True
    )
    add(
        "external_network",
        "PASS" if online and external_network_ok else "FAIL" if online else "WARN",
        "reachable" if online and external_network_ok else "unavailable" if online else "not_checked",
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
    add(
        "payment_preparation",
        "PASS" if recent_payment_failures == 0 else "WARN",
        (
            "recent_failures=0"
            if recent_payment_failures == 0
            else f"recent_failures={recent_payment_failures}"
            if recent_payment_failures is not None
            else "unavailable"
        ),
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
        "application_error_codes": sorted(application_errors),
        "alert_transport": "not_configured",
    }


def _check_states(report: dict[str, Any]) -> dict[str, str]:
    return {
        str(check.get("name")): str(check.get("state"))
        for check in report.get("checks", [])
        if isinstance(check, dict)
    }


def auto_heal_reason(report: dict[str, Any]) -> str | None:
    """Return the one safe restart reason, never a business/external reason."""

    states = _check_states(report)
    if states.get("sqlite") != "PASS" or states.get("migration") != "PASS":
        return None
    if states.get("disk") != "PASS" or states.get("external_network") != "PASS":
        return None
    if states.get("service") == "FAIL":
        return "service_inactive"
    if states.get("max_polling") == "FAIL" and states.get("max_api") == "PASS":
        return "max_polling_stale"
    errors = set(report.get("application_error_codes") or [])
    if (
        states.get("application_health") == "FAIL"
        and errors
        and errors.issubset(HEALABLE_APPLICATION_ERRORS)
    ):
        return "application_runtime_unhealthy"
    return None


def auto_heal_state_path(settings: Settings) -> Path:
    return settings.data_dir / "production-watchdog-autoheal.json"


def _read_auto_heal_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_auto_heal_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".production-watchdog-autoheal-",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _cooldown_remaining_seconds(
    path: Path,
    *,
    now: datetime,
    cooldown: timedelta,
) -> int:
    raw = _read_auto_heal_state(path).get("attempted_at")
    try:
        attempted = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return 0
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=timezone.utc)
    remaining = cooldown - (now - attempted)
    return max(0, int(remaining.total_seconds()))


def _runtime_recovered(report: dict[str, Any]) -> bool:
    states = _check_states(report)
    return all(
        states.get(name) == "PASS"
        for name in (
            "service",
            "application_health",
            "max_polling",
            "max_api",
            "sqlite",
            "migration",
            "disk",
        )
    )


def apply_auto_heal(
    report: dict[str, Any],
    *,
    state_path: Path,
    restart_service: Callable[[], bool],
    refresh_report: Callable[[], dict[str, Any]],
    sleep: Callable[[float], None],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    cooldown: timedelta = AUTO_HEAL_COOLDOWN,
    readiness_attempts: int = AUTO_HEAL_READINESS_ATTEMPTS,
    readiness_delay_seconds: float = AUTO_HEAL_READINESS_DELAY_SECONDS,
) -> dict[str, Any]:
    """Attempt at most one restart for a narrowly proven runtime failure."""

    reason = auto_heal_reason(report)
    if reason is None:
        report["auto_heal"] = {"state": "NOT_ELIGIBLE", "reason": "none"}
        return report
    checked_at = now()
    remaining = _cooldown_remaining_seconds(
        state_path,
        now=checked_at,
        cooldown=cooldown,
    )
    if remaining:
        report["auto_heal"] = {
            "state": "COOLDOWN",
            "reason": reason,
            "remaining_seconds": remaining,
        }
        return report
    state = {
        "attempted_at": checked_at.isoformat(),
        "reason": reason,
        "result": "attempting",
    }
    try:
        _write_auto_heal_state(state_path, state)
    except OSError:
        report["auto_heal"] = {"state": "BLOCKED", "reason": "cooldown_state_unwritable"}
        report["status"] = "CRITICAL"
        report["exit_code"] = 2
        return report
    try:
        restarted = restart_service()
    except Exception:
        restarted = False
    if not restarted:
        state["result"] = "restart_failed"
        try:
            _write_auto_heal_state(state_path, state)
        except OSError:
            pass
        report["auto_heal"] = {"state": "FAILED", "reason": reason}
        report["status"] = "CRITICAL"
        report["exit_code"] = 2
        return report

    latest = report
    for _attempt in range(max(1, readiness_attempts)):
        sleep(readiness_delay_seconds)
        latest = refresh_report()
        if _runtime_recovered(latest):
            state["result"] = "recovered"
            state["recovered_at"] = now().isoformat()
            try:
                _write_auto_heal_state(state_path, state)
            except OSError:
                pass
            latest["auto_heal"] = {"state": "RECOVERED", "reason": reason}
            latest["status"] = "RECOVERED"
            latest["exit_code"] = 0 if all(
                check.get("state") == "PASS" for check in latest.get("checks", [])
            ) else 1
            return latest
    state["result"] = "readiness_failed"
    try:
        _write_auto_heal_state(state_path, state)
    except OSError:
        pass
    latest["auto_heal"] = {"state": "FAILED", "reason": reason}
    latest["status"] = "CRITICAL"
    latest["exit_code"] = 2
    return latest


def render_watchdog(report: dict[str, Any]) -> str:
    lines = [f"STATUS={report['status']}"]
    lines.extend(
        f"{check['name']}={check['state']} {check['detail']}"
        for check in report["checks"]
    )
    if report.get("auto_heal"):
        auto_heal = report["auto_heal"]
        lines.append(
            f"auto_heal={auto_heal.get('state')} reason={auto_heal.get('reason')}"
        )
    lines.append(f"alert_transport={report['alert_transport']}")
    return "\n".join(lines)
