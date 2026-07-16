"""Administrative demo telemetry reporting."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.database import Database


def collect_demo_stats(database: Database) -> dict[str, Any]:
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with database.read() as connection:
        users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        sessions = connection.execute("SELECT COUNT(*) FROM demo_sessions").fetchone()[0]
        successes = connection.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE status='succeeded'"
        ).fetchone()[0]
        attempts = connection.execute("SELECT COUNT(*) FROM generation_attempts").fetchone()[0]
        average_cost = connection.execute(
            "SELECT AVG(estimated_cost) FROM generation_attempts WHERE status='succeeded'"
        ).fetchone()[0]
        daily_cost = connection.execute(
            """SELECT COALESCE(SUM(estimated_cost),0) FROM generation_attempts
               WHERE status='succeeded' AND completed_at>=?""",
            (day_start,),
        ).fetchone()[0]
        converted = connection.execute(
            "SELECT COUNT(*) FROM demo_sessions WHERE converted_to_paid=1"
        ).fetchone()[0]
        technical = connection.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE status='failed_technical'"
        ).fetchone()[0]
        delivery = connection.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE status='delivery_failed'"
        ).fetchone()[0]
        blocked = connection.execute(
            "SELECT COUNT(*) FROM users WHERE status='blocked' OR blocked_until>datetime('now')"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT status,correction,duration_ms,edit_plan_json FROM generation_attempts"
        ).fetchall()
        feedback = dict(connection.execute(
            "SELECT sentiment,COUNT(*) FROM version_feedback GROUP BY sentiment"
        ).fetchall())
        version_count = connection.execute(
            "SELECT COUNT(*) FROM gallery_versions"
        ).fetchone()[0]
    intent_categories: Counter[str] = Counter()
    provider_statuses: Counter[str] = Counter()
    durations: list[int] = []
    correction_count = 0
    for row in rows:
        provider_statuses[row["status"]] += 1
        correction_count += int(bool(row["correction"]))
        if row["duration_ms"] is not None:
            durations.append(int(row["duration_ms"]))
        try:
            category = json.loads(row["edit_plan_json"] or "{}").get("primary_action")
        except (json.JSONDecodeError, AttributeError):
            category = None
        intent_categories[str(category or "legacy_unknown")] += 1
    return {
        "users": users,
        "demo_sessions_started": sessions,
        "successful_demo_results": successes,
        "average_attempts_per_session": round(attempts / sessions, 3) if sessions else 0.0,
        "average_estimated_cost_rub": round(float(average_cost), 4) if average_cost is not None else None,
        "daily_estimated_cost_rub": round(float(daily_cost), 4),
        "conversion_to_paid": round(converted / sessions, 4) if sessions else None,
        "technical_errors": technical,
        "delivery_failures": delivery,
        "blocked_users": blocked,
        "intent_categories": dict(intent_categories),
        "correction_attempts": correction_count,
        "feedback": {
            "positive": int(feedback.get("positive", 0)),
            "negative": int(feedback.get("negative", 0)),
        },
        "average_generation_duration_ms": (
            round(sum(durations) / len(durations), 1) if durations else None
        ),
        "gallery_versions": version_count,
        "provider_statuses": dict(provider_statuses),
    }
