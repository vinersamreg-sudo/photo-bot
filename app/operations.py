"""Privacy-safe launch readiness report for the closed pilot."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    service_active = _systemd_active() if settings.app_env == "production" else True
    backup_age = _age_hours(latest.get("created_at")) if latest else None
    poll_age = _age_hours(poll[0]) if poll else None
    cleanup_age = _age_hours(cleanup.get("completed_at")) if cleanup else None
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
        and int(migration) >= 6
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
    return {
        "runtime": {
            "environment": settings.app_env,
            "transport": settings.max_transport_mode,
            "observe_only": settings.max_poll_observe_only,
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
        },
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
            "public_launch_ready": False,
            "mode": "closed_pilot",
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
