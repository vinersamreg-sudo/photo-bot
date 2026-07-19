"""Privacy-safe commercial, pilot, storage and economics status reports."""

from __future__ import annotations

import json
import os
import socket
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import Database
from app.maintenance import _referenced_private_files
from app.operations import collect_launch_status


def _json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def payment_status(settings: Settings, database: Database) -> dict[str, Any]:
    with database.read() as connection:
        order_rows = connection.execute(
            "SELECT status,COUNT(*) AS count,COALESCE(SUM(amount_minor),0) AS amount FROM payment_orders GROUP BY status"
        ).fetchall()
        refund_rows = connection.execute(
            "SELECT status,COUNT(*) AS count,COALESCE(SUM(amount_minor),0) AS amount FROM refund_intents GROUP BY status"
        ).fetchall()
        rejected = connection.execute(
            "SELECT COUNT(*) FROM payment_events WHERE status='rejected'"
        ).fetchone()[0]
        pending_delivery = connection.execute(
            "SELECT COUNT(*) FROM payment_orders WHERE status='delivery_pending'"
        ).fetchone()[0]
        duplicate_events = connection.execute(
            "SELECT COUNT(*) FROM payment_audit WHERE event_type='duplicate_payment'"
        ).fetchone()[0]
    orders = {
        str(row["status"]): {"count": int(row["count"]), "amount_rub": int(row["amount"]) / 100}
        for row in order_rows
    }
    refunds = {
        str(row["status"]): {"count": int(row["count"]), "amount_rub": int(row["amount"]) / 100}
        for row in refund_rows
    }
    listener_reachable = False
    if settings.payments_enabled and settings.payment_webhook_enabled:
        try:
            with socket.create_connection(
                (settings.payment_webhook_host, settings.payment_webhook_port), timeout=0.5
            ):
                listener_reachable = True
        except OSError:
            pass
    return {
        "enabled": settings.payments_enabled,
        "provider": settings.payment_provider,
        "mode": settings.robokassa_mode,
        "production_approved": settings.robokassa_production_approved,
        "webhook_enabled": settings.payment_webhook_enabled,
        "webhook_listener_reachable": listener_reachable,
        "refund_execution_enabled": settings.payment_refunds_enabled,
        "credentials_present": bool(
            settings.robokassa_merchant_login
            and settings.robokassa_password1
            and settings.robokassa_password2
        ),
        "orders": orders,
        "refunds": refunds,
        "rejected_webhooks": int(rejected),
        "duplicate_payment_events": int(duplicate_events),
        "original_delivery_pending": int(pending_delivery),
    }


def pilot_status(settings: Settings, database: Database) -> dict[str, Any]:
    with database.read() as connection:
        allowed_users_seen = connection.execute(
            "SELECT COUNT(*) FROM users WHERE platform='max'"
        ).fetchone()[0]
        successful_sessions = connection.execute(
            """SELECT COUNT(DISTINCT session_id) FROM generation_attempts
               WHERE status='succeeded'"""
        ).fetchone()[0]
        errors = connection.execute(
            "SELECT COUNT(*) FROM product_events WHERE event_type='error'"
        ).fetchone()[0]
    return {
        "stage": settings.pilot_user_limit,
        "configured_pilot_count": len(settings.max_pilot_user_ids),
        "owner_configured": bool(settings.max_owner_user_ids),
        "handlers_enabled": not settings.max_poll_observe_only,
        "max_users_seen": int(allowed_users_seen),
        "successful_sessions": int(successful_sessions),
        "recorded_errors": int(errors),
        "ready_for_configured_stage": bool(
            settings.max_owner_user_ids
            and len(settings.max_pilot_user_ids) >= settings.pilot_user_limit
            and settings.pilot_user_limit in {0, 5, 10, 20}
        ),
    }


def storage_status(settings: Settings, database: Database) -> dict[str, Any]:
    usage = shutil.disk_usage(settings.base_dir)
    referenced_paths = _referenced_private_files(database)
    orphans = [
        path for path in settings.users_dir.rglob("*")
        if path.is_file() and not path.is_symlink() and path.resolve() not in referenced_paths
    ] if settings.users_dir.is_dir() else []
    with database.read() as connection:
        referenced = connection.execute(
            "SELECT COUNT(*) FROM gallery_versions WHERE original_path IS NOT NULL OR preview_watermarked_path IS NOT NULL"
        ).fetchone()[0]
    return {
        "free_mb": usage.free // (1024 * 1024),
        "minimum_free_mb": settings.disk_min_free_mb,
        "referenced_versions": int(referenced),
        "orphan_candidates": len(orphans),
    }


def backup_status(settings: Settings) -> dict[str, Any]:
    return {
        "latest": _json(settings.backup_dir_path / "latest.json"),
        "restore": _json(settings.backup_dir_path / "restore_status.json"),
        "offsite": _json(settings.backup_dir_path / "offsite_status.json"),
    }


def cleanup_status(settings: Settings) -> dict[str, Any]:
    return _json(settings.data_dir / "maintenance_last.json") or {"completed_at": None}


def host_status(settings: Settings, database: Database) -> dict[str, Any]:
    memory_total_mb = None
    memory_available_mb = None
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) // 1024
        memory_total_mb = values.get("MemTotal")
        memory_available_mb = values.get("MemAvailable")
    except (OSError, ValueError, IndexError):
        pass
    try:
        load_1m = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        load_1m = None
    usage = shutil.disk_usage(settings.base_dir)
    with database.read() as connection:
        queue = connection.execute(
            """SELECT
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) AS processing
               FROM generation_attempts"""
        ).fetchone()
    return {
        "cpu_count": os.cpu_count(),
        "load_1m": load_1m,
        "memory_total_mb": memory_total_mb,
        "memory_available_mb": memory_available_mb,
        "disk_free_mb": usage.free // (1024 * 1024),
        "generation_queue_pending": int(queue["pending"] or 0),
        "generation_queue_processing": int(queue["processing"] or 0),
        "global_generation_limit": settings.global_max_concurrent_generations,
    }


def cost_status(settings: Settings, database: Database) -> dict[str, Any]:
    with database.read() as connection:
        generation = connection.execute(
            """SELECT COUNT(*) AS count,COALESCE(SUM(estimated_cost),0) AS cost
               FROM generation_attempts WHERE status='succeeded'"""
        ).fetchone()
        revenue = connection.execute(
            """SELECT COALESCE(SUM(amount_minor),0) FROM payment_orders
               WHERE status IN ('paid','delivery_pending','delivered','refund_pending','partially_refunded')"""
        ).fetchone()[0]
        refunded = connection.execute(
            "SELECT COALESCE(SUM(refunded_amount_minor),0) FROM payment_orders"
        ).fetchone()[0]
        paid_orders = connection.execute(
            """SELECT COUNT(*) FROM payment_orders
               WHERE status IN ('paid','delivery_pending','delivered','refund_pending','partially_refunded','refunded')"""
        ).fetchone()[0]
        paid_users = connection.execute(
            """SELECT COUNT(DISTINCT user_id) FROM payment_orders
               WHERE status IN ('paid','delivery_pending','delivered','refund_pending','partially_refunded','refunded')"""
        ).fetchone()[0]
        starts = connection.execute(
            "SELECT COUNT(*) FROM product_events WHERE event_type='start'"
        ).fetchone()[0]
        corrections = connection.execute(
            "SELECT COUNT(*) FROM product_events WHERE event_type='correction_started'"
        ).fetchone()[0]
        repeats = connection.execute(
            "SELECT COUNT(*) FROM product_events WHERE event_type='repeat_started'"
        ).fetchone()[0]
        correction_cost = connection.execute(
            """SELECT COUNT(*) AS count,COALESCE(SUM(estimated_cost),0) AS cost
               FROM generation_attempts WHERE status='succeeded' AND correction=1"""
        ).fetchone()
        repeat_cost = connection.execute(
            """SELECT COUNT(*) AS count,COALESCE(SUM(estimated_cost),0) AS cost
               FROM generation_attempts
               WHERE status='succeeded' AND correction=0 AND source_version_id IS NOT NULL"""
        ).fetchone()
    generation_cost = float(generation["cost"] or 0)
    net_revenue = (int(revenue) - int(refunded)) / 100
    estimated_commission = int(revenue) / 100 * settings.robokassa_commission_percent / 100
    return {
        "successful_generations": int(generation["count"]),
        "paid_orders": int(paid_orders),
        "paid_users": int(paid_users),
        "estimated_generation_cost_rub": round(generation_cost, 2),
        "recognized_revenue_rub": round(int(revenue) / 100, 2),
        "refunded_rub": round(int(refunded) / 100, 2),
        "net_revenue_rub": round(net_revenue, 2),
        "robokassa_commission_percent": settings.robokassa_commission_percent,
        "estimated_robokassa_commission_rub": round(estimated_commission, 2),
        "estimated_contribution_rub": round(
            net_revenue - generation_cost - estimated_commission, 2
        ),
        "conversion_percent": round(int(paid_orders) * 100 / int(starts), 2) if starts else None,
        "arpu_rub": round(net_revenue / int(paid_users), 2) if paid_users else None,
        "corrections": int(corrections),
        "repeats": int(repeats),
        "average_corrections_per_paid_user": (
            round(int(corrections) / int(paid_users), 2) if paid_users else None
        ),
        "average_correction_cost_rub": (
            round(float(correction_cost["cost"] or 0) / int(correction_cost["count"]), 2)
            if correction_cost["count"] else None
        ),
        "average_repeat_cost_rub": (
            round(float(repeat_cost["cost"] or 0) / int(repeat_cost["count"]), 2)
            if repeat_cost["count"] else None
        ),
        "estimated_cost_per_result_rub": round(
            generation_cost / int(generation["count"]), 2
        ) if generation["count"] else None,
        "estimated_cost_per_paid_order_rub": round(
            generation_cost / int(paid_orders), 2
        ) if paid_orders else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def health_report(settings: Settings, *, online: bool) -> dict[str, Any]:
    database = Database(settings.database_path)
    return {
        "launch": collect_launch_status(settings, online=online),
        "pilot": pilot_status(settings, database),
        "payments": payment_status(settings, database),
        "storage": storage_status(settings, database),
        "backup": backup_status(settings),
        "cleanup": cleanup_status(settings),
        "cost": cost_status(settings, database),
        "host": host_status(settings, database),
    }
