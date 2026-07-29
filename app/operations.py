"""Privacy-safe launch readiness report for the closed pilot."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.database import Database
from app.openai_client import check_openai_connection, create_openai_client


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _age_hours(value: object) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 3600)
    except (TypeError, ValueError):
        return None


def openai_budget_status(settings: Settings) -> dict[str, Any]:
    """Return the last manual balance confirmation without claiming live data."""

    age_hours = (
        _age_hours(settings.openai_balance_confirmed_at)
        if settings.openai_balance_confirmed_at
        else None
    )
    balance = settings.openai_balance_usd
    low_balance_warning = bool(
        balance is not None and balance <= settings.openai_balance_warning_usd
    )
    critical_balance = bool(
        balance is not None and balance <= settings.openai_balance_critical_usd
    )
    confirmation_stale = bool(
        age_hours is None or age_hours > settings.openai_balance_max_age_hours
    )
    return {
        "source": "manual_confirmation",
        "live_balance_claimed": False,
        "openai_balance_confirmed_at": (
            settings.openai_balance_confirmed_at or None
        ),
        "openai_balance_usd": balance,
        "confirmation_age_hours": (
            round(age_hours, 2) if age_hours is not None else None
        ),
        "confirmation_stale": confirmation_stale,
        "warning_threshold_usd": settings.openai_balance_warning_usd,
        "critical_threshold_usd": settings.openai_balance_critical_usd,
        "low_balance_warning": low_balance_warning,
        "critical_balance": critical_balance,
        "image_requests_enabled": bool(
            settings.openai_image_requests_enabled and not critical_balance
        ),
        "daily_request_limit": settings.demo_daily_generation_limit,
        "daily_estimated_cost_limit_rub": settings.demo_daily_cost_limit_rub,
        "estimated_cost_per_request_rub": (
            settings.demo_estimated_cost_rub_per_generation
        ),
    }


def _systemd_active() -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", "photo-bot.service"],
            check=False,
        ).returncode == 0
    except OSError:
        return False


def _connectivity(settings: Settings, online: bool) -> tuple[bool | None, bool | None]:
    if not online:
        return None, None
    max_ok = False
    openai_ok = False
    try:
        from app.max_transport import MaxApiClient

        client = MaxApiClient(
            settings.max_bot_token,
            settings.max_api_base_url,
            timeout_seconds=30,
            ca_bundle=settings.max_ca_bundle_path,
            media_host_suffixes=settings.max_media_host_suffixes,
        )
        try:
            client.get_me()
            max_ok = True
        finally:
            client.close()
    except Exception:
        max_ok = False
    try:
        check_openai_connection(
            create_openai_client(settings), settings.openai_image_model
        )
        openai_ok = True
    except Exception:
        openai_ok = False
    return max_ok, openai_ok


def _site_moderation_status(online: bool) -> dict[str, Any]:
    required = (
        "/",
        "/contacts.html",
        "/legal/offer.html",
        "/legal/privacy.html",
        "/legal/personal-data.html",
        "/legal/payment-refund.html",
        "/legal/terms.html",
        "/robots.txt",
        "/sitemap.xml",
        "/assets/favicon.svg",
    )
    if not online:
        return {
            "checked": False,
            "required_routes_ok": None,
            "price_49_consistent": None,
            "legacy_price_149_absent": None,
            "https_redirect_ok": None,
            "www_redirect_ok": None,
            "hsts_present": None,
            "moderation_ready": False,
        }
    route_status: dict[str, int | None] = {}
    bodies: list[str] = []
    https_redirect_ok = False
    www_redirect_ok = False
    hsts_present = False
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            for path in required:
                response = client.get(f"https://ravuna.ru{path}")
                route_status[path] = response.status_code
                if response.status_code == 200 and response.headers.get(
                    "content-type", ""
                ).lower().startswith(("text/html", "text/plain", "application/xml")):
                    bodies.append(response.text)
                if path == "/":
                    hsts_present = bool(response.headers.get("strict-transport-security"))
            http_response = client.get("http://ravuna.ru/")
            https_redirect_ok = bool(
                http_response.status_code in {301, 302, 307, 308}
                and http_response.headers.get("location", "").startswith(
                    "https://ravuna.ru"
                )
            )
            www_response = client.get("https://www.ravuna.ru/")
            www_redirect_ok = bool(
                www_response.status_code in {301, 302, 307, 308}
                and www_response.headers.get("location", "").startswith(
                    "https://ravuna.ru"
                )
            )
    except (httpx.HTTPError, OSError):
        pass
    combined = "\n".join(bodies)
    routes_ok = bool(route_status) and all(value == 200 for value in route_status.values())
    price_49 = "49 ₽" in combined
    price_149_absent = "149 ₽" not in combined
    ready = bool(
        routes_ok
        and price_49
        and price_149_absent
        and https_redirect_ok
        and www_redirect_ok
        and hsts_present
    )
    return {
        "checked": True,
        "route_status": route_status,
        "required_routes_ok": routes_ok,
        "price_49_consistent": price_49,
        "legacy_price_149_absent": price_149_absent,
        "https_redirect_ok": https_redirect_ok,
        "www_redirect_ok": www_redirect_ok,
        "hsts_present": hsts_present,
        "moderation_ready": ready,
    }


def _owner_e2e_status(settings: Settings, database: Database) -> dict[str, Any]:
    if not settings.max_owner_user_ids:
        return {"configured": False, "successful_attempts": 0, "ready": False}
    placeholders = ",".join("?" for _ in settings.max_owner_user_ids)
    with database.read() as connection:
        owner_rows = connection.execute(
            f"SELECT id FROM users WHERE platform='max' AND platform_user_id IN ({placeholders})",
            settings.max_owner_user_ids,
        ).fetchall()
        owner_ids = [str(row[0]) for row in owner_rows]
        succeeded = 0
        gallery_versions = 0
        if owner_ids:
            owner_placeholders = ",".join("?" for _ in owner_ids)
            succeeded = int(connection.execute(
                f"SELECT COUNT(*) FROM generation_attempts WHERE user_id IN ({owner_placeholders}) AND status='succeeded'",
                owner_ids,
            ).fetchone()[0])
            gallery_versions = int(connection.execute(
                f"""SELECT COUNT(*) FROM gallery_versions v
                    JOIN generation_attempts a ON a.id=v.attempt_id
                    WHERE a.user_id IN ({owner_placeholders}) AND v.status='succeeded'
                      AND v.original_path IS NOT NULL AND v.preview_watermarked_path IS NOT NULL""",
                owner_ids,
            ).fetchone()[0])
    ready = len(owner_rows) == 1 and succeeded >= 5 and gallery_versions >= 5
    return {
        "configured": True,
        "owner_record_count": len(owner_rows),
        "successful_attempts": succeeded,
        "complete_gallery_versions": gallery_versions,
        "minimum_required": 5,
        "ready": ready,
    }


def collect_launch_status(settings: Settings, *, online: bool = True) -> dict[str, Any]:
    database = Database(settings.database_path)
    with database.read() as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        migration = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
        ).fetchone()[0]
        active_attempts = connection.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE status IN ('pending','processing')"
        ).fetchone()[0]
        active_dialogs = connection.execute(
            "SELECT COUNT(*) FROM max_dialogs WHERE state='processing'"
        ).fetchone()[0]
        metrics = connection.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(AVG(duration_ms),0) AS avg_duration_ms,
                      COALESCE(SUM(estimated_cost),0) AS estimated_cost
               FROM generation_attempts
               WHERE status='succeeded' AND date(started_at)=date('now')"""
        ).fetchone()
        attempt_health = connection.execute(
            """SELECT
                   SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                   SUM(CASE WHEN status NOT IN ('pending','processing','succeeded') THEN 1 ELSE 0 END) AS failed
               FROM generation_attempts WHERE date(started_at)=date('now')"""
        ).fetchone()
        errors_today = connection.execute(
            "SELECT COUNT(*) FROM product_events WHERE event_type='error' AND date(created_at)=date('now')"
        ).fetchone()[0]
        delivery_today = connection.execute(
            "SELECT COUNT(*) FROM product_events WHERE event_type='result_delivered' AND date(created_at)=date('now')"
        ).fetchone()[0]
        delivery_failures = connection.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE error_type='delivery_failed' AND date(started_at)=date('now')"
        ).fetchone()[0]
        provider_failures = connection.execute(
            """SELECT COUNT(*) FROM generation_attempts
               WHERE status='failed_technical' AND date(started_at)=date('now')"""
        ).fetchone()[0]
        stale_processing = connection.execute(
            """SELECT COUNT(*) FROM generation_attempts
               WHERE status IN ('pending','processing')
                 AND started_at < datetime('now','-10 minutes')"""
        ).fetchone()[0]
        rejected_callbacks = connection.execute(
            "SELECT COUNT(*) FROM payment_events WHERE status='rejected'"
        ).fetchone()[0]
        paid_without_grant = connection.execute(
            """SELECT COUNT(*) FROM payment_orders o
               LEFT JOIN continuation_pack_grants g ON g.payment_order_id=o.id
               WHERE o.status IN ('paid','delivery_pending','delivered')
                 AND g.id IS NULL"""
        ).fetchone()[0]
        paid_original_retry = connection.execute(
            "SELECT COUNT(*) FROM payment_orders WHERE status='delivery_pending'"
        ).fetchone()[0]
        pending_intent_mismatch = connection.execute(
            """SELECT COUNT(*) FROM payment_intents i
               JOIN payment_orders o ON o.intent_id=i.id
               WHERE i.status='pending' AND o.status='expired'"""
        ).fetchone()[0]
        funnel = connection.execute(
            """SELECT
                   COUNT(DISTINCT CASE WHEN event_type='start' THEN id END) AS starts,
                   COUNT(DISTINCT CASE WHEN event_type='photo_uploaded' THEN session_id END) AS uploaded_sessions,
                   COUNT(DISTINCT CASE WHEN event_type='prompt_submitted' THEN session_id END) AS prompted_sessions,
                   COUNT(DISTINCT CASE WHEN event_type='result_delivered' THEN session_id END) AS delivered_sessions,
                   SUM(CASE WHEN event_type='correction_started' THEN 1 ELSE 0 END) AS corrections,
                   SUM(CASE WHEN event_type='repeat_started' THEN 1 ELSE 0 END) AS repeats,
                   SUM(CASE WHEN event_type='feedback_positive' THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN event_type='feedback_negative' THEN 1 ELSE 0 END) AS negative,
                   SUM(CASE WHEN event_type='prompt_submitted' AND parser_fallback=1 THEN 1 ELSE 0 END) AS fallbacks,
                   SUM(CASE WHEN event_type='prompt_submitted' THEN 1 ELSE 0 END) AS prompts
               FROM product_events WHERE date(created_at)=date('now')"""
        ).fetchone()
        poll = connection.execute(
            "SELECT updated_at FROM max_transport_state WHERE name='poll_last_success'"
        ).fetchone()
    latest = _read_json(settings.backup_dir_path / "latest.json")
    restore = _read_json(settings.backup_dir_path / "restore_status.json")
    offsite = _read_json(settings.backup_dir_path / "offsite_status.json")
    cleanup = _read_json(settings.data_dir / "maintenance_last.json")
    usage = shutil.disk_usage(settings.base_dir)
    max_connected, openai_connected = _connectivity(settings, online)
    site = _site_moderation_status(online)
    owner_e2e = _owner_e2e_status(settings, database)
    service_active = _systemd_active() if settings.app_env == "production" else True
    backup_age = _age_hours(latest.get("created_at")) if latest else None
    poll_age = _age_hours(poll[0]) if poll else None
    cleanup_age = _age_hours(cleanup.get("completed_at")) if cleanup else None
    budget = openai_budget_status(settings)
    restore_matches = bool(
        latest and restore and restore.get("restore_ok") is True
        and restore.get("backup_name") == latest.get("backup_name")
    )
    offsite_matches = bool(
        latest and offsite and offsite.get("backup_name") == latest.get("backup_name")
    )
    runtime_ready = (
        settings.openai_image_model == "gpt-image-2"
        and bool(settings.openai_api_key)
        and settings.max_transport_mode == "polling"
        and bool(settings.max_bot_token)
        and bool(settings.max_owner_user_ids)
        and quick_check == "ok"
        and int(migration) >= 8
        and active_attempts == 0
        and active_dialogs == 0
        and usage.free >= settings.disk_min_free_mb * 1024 * 1024
        and service_active
        and (max_connected is not False)
        and (openai_connected is not False)
        and poll_age is not None
        and poll_age * 3600 <= settings.max_poll_max_stale_seconds
    )
    operational_ready = bool(
        runtime_ready
        and backup_age is not None
        and backup_age <= settings.backup_max_age_hours
        and restore_matches
        and offsite_matches
        and cleanup_age is not None
        and cleanup_age <= settings.backup_max_age_hours
        and cleanup.get("remaining_orphan_count") == 0
    )
    sandbox_evidence = _read_json(settings.data_dir / "robokassa_sandbox_evidence.json")
    sandbox_e2e_verified = bool(
        sandbox_evidence
        and sandbox_evidence.get("verified") is True
        and sandbox_evidence.get("mode") == "sandbox"
    )
    credentials_present = bool(
        settings.robokassa_merchant_login
        and settings.robokassa_password1
        and settings.robokassa_password2
    )
    sandbox_ready = bool(
        operational_ready
        and credentials_present
        and settings.payment_provider == "robokassa"
        and settings.robokassa_mode == "sandbox"
        and settings.payment_result_url.startswith("https://")
        and settings.payment_success_url.startswith("https://")
        and settings.payment_fail_url.startswith("https://")
    )
    production_payment_ready = bool(
        operational_ready
        and sandbox_e2e_verified
        and credentials_present
        and settings.payment_provider == "robokassa"
        and settings.robokassa_mode == "production"
        and settings.robokassa_production_approved
        and settings.payment_result_url.startswith("https://")
        and settings.payment_refunds_enabled
    )
    pilot_5_ready = bool(
        operational_ready
        and owner_e2e["ready"]
        and len(settings.max_pilot_user_ids) >= 5
        and settings.max_poll_observe_only
        and settings.pilot_user_limit == 0
        and budget["image_requests_enabled"]
    )
    public_launch_ready = bool(
        operational_ready
        and settings.max_public_access_enabled
        and not settings.max_poll_observe_only
    )
    alerts: dict[str, list[dict[str, str]]] = {"p0": [], "p1": []}

    def add_alert(priority: str, code: str, message: str) -> None:
        alerts[priority].append({"code": code, "message": message})

    if not service_active:
        add_alert("p0", "process_down", "photo-bot.service is not active")
    if poll_age is None or poll_age * 3600 > settings.max_poll_max_stale_seconds:
        add_alert("p0", "polling_disconnected", "MAX polling contact is stale")
    if quick_check != "ok":
        add_alert("p0", "sqlite_failure", "SQLite quick_check failed")
    if rejected_callbacks:
        add_alert("p0", "payment_callback_failure", "Rejected payment callbacks exist")
    if paid_without_grant:
        add_alert("p0", "paid_without_grant", "A paid order has no package grant")
    if paid_original_retry:
        add_alert(
            "p0",
            "paid_original_retry",
            "A paid order is waiting for original delivery",
        )
    if not budget["image_requests_enabled"]:
        add_alert(
            "p0",
            "image_requests_disabled",
            "Image processing is globally disabled by the budget guard",
        )
    if usage.free < settings.disk_min_free_mb * 1024 * 1024:
        add_alert("p0", "disk_critical", "Free disk space is below the minimum")
    if budget["low_balance_warning"]:
        add_alert(
            "p1",
            "openai_balance_low",
            "The last manually confirmed OpenAI balance is below the warning threshold",
        )
    if budget["confirmation_stale"]:
        add_alert(
            "p1",
            "openai_balance_confirmation_stale",
            "The manual OpenAI balance confirmation is missing or stale",
        )
    if int(provider_failures) + int(delivery_failures) >= 3:
        add_alert(
            "p1",
            "repeated_delivery_or_provider_failures",
            "Repeated provider or MAX delivery failures were observed today",
        )
    if pending_intent_mismatch:
        add_alert(
            "p1",
            "pending_payment_mismatch",
            "Pending payment intents are linked to expired orders",
        )
    if stale_processing:
        add_alert(
            "p1", "stale_processing", "Processing records are older than ten minutes"
        )
    if backup_age is None or backup_age > settings.backup_max_age_hours:
        add_alert("p1", "stale_backup", "The latest backup is missing or stale")
    return {
        "runtime": {
            "environment": settings.app_env,
            "transport": settings.max_transport_mode,
            "observe_only": settings.max_poll_observe_only,
            "public_access_enabled": settings.max_public_access_enabled,
            "owner_allowlist_configured": bool(settings.max_owner_user_ids),
            "pilot_limit": settings.pilot_user_limit,
            "pilot_configured_count": len(settings.max_pilot_user_ids),
            "router_enabled": settings.processing_mode_router_enabled,
            "service_active": service_active,
        },
        "provider": {
            "model": settings.openai_image_model,
            "configured": bool(settings.openai_api_key),
            "connected": openai_connected,
            **budget,
        },
        "payments": {
            "enabled": settings.payments_enabled,
            "provider": settings.payment_provider,
            "mode": settings.robokassa_mode,
            "webhook_enabled": settings.payment_webhook_enabled,
            "refund_execution_enabled": settings.payment_refunds_enabled,
            "production_approved": settings.robokassa_production_approved,
            "credentials_present": credentials_present,
            "sandbox_e2e_verified": sandbox_e2e_verified,
        },
        "site": site,
        "owner_e2e": owner_e2e,
        "database": {"quick_check": quick_check, "migration": int(migration)},
        "storage": {"free_mb": usage.free // (1024 * 1024), "minimum_free_mb": settings.disk_min_free_mb},
        "backup": {
            "latest_age_hours": round(backup_age, 2) if backup_age is not None else None,
            "restore_tested": restore_matches,
            "offsite_copied": offsite_matches,
        },
        "cleanup": {
            "last_run_age_hours": round(cleanup_age, 2) if cleanup_age is not None else None,
            "orphan_candidate_count": cleanup.get("orphan_candidate_count") if cleanup else None,
            "remaining_orphan_count": cleanup.get("remaining_orphan_count") if cleanup else None,
        },
        "processing": {"active_attempts": active_attempts, "active_dialogs": active_dialogs},
        "monitoring": {
            "alerts": alerts,
            "p0_count": len(alerts["p0"]),
            "p1_count": len(alerts["p1"]),
            "delivery_failures_today": int(delivery_failures),
            "provider_failures_today": int(provider_failures),
            "pending_intent_mismatch": int(pending_intent_mismatch),
            "stale_processing": int(stale_processing),
        },
        "today": {
            "successful_generations": int(metrics["total"]),
            "average_duration_ms": round(float(metrics["avg_duration_ms"]), 1),
            "estimated_cost_rub": round(float(metrics["estimated_cost"]), 4),
            "errors": errors_today,
            "deliveries": delivery_today,
            "delivery_failures": delivery_failures,
            "error_rate": round(
                (attempt_health["failed"] or 0)
                / ((attempt_health["succeeded"] or 0) + (attempt_health["failed"] or 0)),
                4,
            ) if ((attempt_health["succeeded"] or 0) + (attempt_health["failed"] or 0)) else 0.0,
            "starts": int(funnel["starts"] or 0),
            "photo_sessions": int(funnel["uploaded_sessions"] or 0),
            "prompt_sessions": int(funnel["prompted_sessions"] or 0),
            "first_result_completion_rate": round(
                (funnel["delivered_sessions"] or 0) / (funnel["uploaded_sessions"] or 1), 4
            ) if funnel["uploaded_sessions"] else 0.0,
            "corrections": int(funnel["corrections"] or 0),
            "repeats": int(funnel["repeats"] or 0),
            "feedback_positive": int(funnel["positive"] or 0),
            "feedback_negative": int(funnel["negative"] or 0),
            "parser_fallback_rate": round(
                (funnel["fallbacks"] or 0) / (funnel["prompts"] or 1), 4
            ) if funnel["prompts"] else 0.0,
        },
        "max": {
            "connected": max_connected,
            "last_poll_age_hours": round(poll_age, 3) if poll_age is not None else None,
        },
        "readiness": {
            "runtime_ready": runtime_ready,
            "operational_ready": operational_ready,
            "site_moderation_ready": bool(site["moderation_ready"]),
            "robokassa_sandbox_ready": sandbox_ready,
            "robokassa_production_ready": production_payment_ready,
            "owner_e2e_ready": bool(owner_e2e["ready"]),
            "pilot_5_ready": pilot_5_ready,
            "public_launch_ready": public_launch_ready,
            "mode": (
                "public"
                if settings.max_public_access_enabled
                else "closed_pilot"
            ),
        },
    }


def print_launch_status(settings: Settings, *, strict: bool) -> int:
    try:
        report = collect_launch_status(settings)
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"readiness": {"operational_ready": False}, "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (not strict or report["readiness"]["operational_ready"]) else 1
