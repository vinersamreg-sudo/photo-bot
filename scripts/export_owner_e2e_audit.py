"""Export a redacted, read-only audit of the latest owner image requests."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from app.config import Settings, load_settings
from app.database import Database
from app.edit_intent import SceneIntent
from app.maintenance import _referenced_private_files


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {prefix: value} if prefix else {}
    result: dict[str, Any] = {}
    for key, nested in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(nested, Mapping):
            result.update(_flatten(nested, name))
        else:
            result[name] = nested
    return result


def _changed_scene(current: Mapping[str, Any], parent: Mapping[str, Any] | None) -> dict[str, Any]:
    baseline = parent if parent is not None else asdict(SceneIntent())
    current_flat = _flatten(current)
    baseline_flat = _flatten(baseline)
    return {
        key: value
        for key, value in current_flat.items()
        if baseline_flat.get(key) != value
    }


def _image_facts(path_value: str | None) -> dict[str, Any] | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        return {"available": False}
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    with Image.open(path) as image:
        width, height = image.size
        image_format = image.format
    return {
        "available": True,
        "sha256": digest.hexdigest(),
        "size_bytes": path.stat().st_size,
        "width": width,
        "height": height,
        "format": image_format,
    }


def _version_number(connection: Any, version_id: str | None) -> int | None:
    if not version_id:
        return None
    row = connection.execute(
        "SELECT version_number FROM gallery_versions WHERE id=?", (version_id,)
    ).fetchone()
    return int(row[0]) if row else None


def _orphan_count(settings: Settings, database: Database) -> int:
    referenced = _referenced_private_files(database)
    if not settings.users_dir.is_dir():
        return 0
    return sum(
        1
        for candidate in settings.users_dir.rglob("*")
        if candidate.is_file()
        and not candidate.is_symlink()
        and candidate.resolve() not in referenced
    )


def build_report(settings: Settings, *, limit: int = 5) -> dict[str, Any]:
    """Return a redacted report; no platform/internal IDs or private paths escape."""

    if limit < 1 or limit > 5:
        raise ValueError("limit must be between 1 and 5")
    if not settings.max_owner_user_ids:
        raise RuntimeError("Owner allowlist is not configured")

    database = Database(settings.database_path)
    with database.read() as connection:
        placeholders = ",".join("?" for _ in settings.max_owner_user_ids)
        owners = connection.execute(
            f"SELECT id FROM users WHERE platform='max' AND platform_user_id IN ({placeholders})",
            settings.max_owner_user_ids,
        ).fetchall()
        if len(owners) != 1:
            raise RuntimeError("Exactly one configured owner must exist in production")
        owner_user_id = owners[0]["id"]
        rows = connection.execute(
            """SELECT a.*,v.id AS gallery_version_id,v.version_number AS gallery_version_number,
                      v.favorite AS version_favorite,i.current_best_version_id,
                      i.favorite AS item_favorite,s.successful_generations,s.max_generations,
                      s.status AS session_status
               FROM generation_attempts a
               LEFT JOIN gallery_versions v ON v.attempt_id=a.id
               LEFT JOIN gallery_items i ON i.id=v.gallery_item_id
               JOIN demo_sessions s ON s.id=a.session_id
               WHERE a.user_id=? AND a.status='succeeded'
               ORDER BY a.started_at DESC LIMIT ?""",
            (owner_user_id, limit),
        ).fetchall()
        if len(rows) != limit:
            raise RuntimeError(f"Expected {limit} successful owner attempts, found {len(rows)}")
        rows = list(reversed(rows))

        attempts: list[dict[str, Any]] = []
        for sequence, row in enumerate(rows, start=1):
            plan = _json_object(row["edit_plan_json"])
            scene = plan.get("scene") if isinstance(plan.get("scene"), Mapping) else {}
            parent_plan: dict[str, Any] = {}
            if row["parent_version_id"]:
                parent = connection.execute(
                    "SELECT edit_plan_json FROM gallery_versions WHERE id=?",
                    (row["parent_version_id"],),
                ).fetchone()
                parent_plan = _json_object(parent[0]) if parent else {}
            parent_scene = (
                parent_plan.get("scene")
                if isinstance(parent_plan.get("scene"), Mapping)
                else None
            )
            changed = _changed_scene(scene, parent_scene)
            if plan.get("mode") == "repeat":
                changed = {}
            input_facts = _image_facts(row["source_path"])
            if input_facts is not None:
                input_facts["source_version_number"] = _version_number(
                    connection, row["source_version_id"]
                )
            output_facts = _image_facts(row["original_result_path"])
            attempts.append(
                {
                    "sequence": sequence,
                    "russian_text": row["prompt"],
                    "scene_intent": scene,
                    "primary_action": plan.get("primary_action"),
                    "mode": plan.get("mode"),
                    "changed_fields": changed,
                    "inherited_constraints": plan.get("inherited_constraints") or [],
                    "parent_version_number": _version_number(
                        connection, row["parent_version_id"]
                    ),
                    "source_version_number": _version_number(
                        connection, row["source_version_id"]
                    ),
                    "gallery_version_number": row["gallery_version_number"],
                    "technical_prompt": row["provider_prompt"],
                    "input_image": input_facts,
                    "output_image": output_facts,
                    "status": row["status"],
                    "http_status": None,
                    "http_status_note": "Provider HTTP status is not persisted by the SDK integration.",
                    "duration_ms": row["duration_ms"],
                    "provider_request_id": row["external_request_id"],
                    "output_size_bytes": row["output_size_bytes"],
                    "gallery_version_created": bool(row["gallery_version_id"]),
                    "demo_quota_charged": bool(row["gallery_version_id"]),
                    "estimated_cost_rub": row["estimated_cost"],
                    "correction": bool(row["correction"]),
                    "prompt_builder_version": row["prompt_builder_version"],
                    "favorite": bool(row["version_favorite"] or row["item_favorite"]),
                    "current_best": row["current_best_version_id"] == row["gallery_version_id"],
                }
            )

        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        pending = connection.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE status IN ('pending','processing')"
        ).fetchone()[0]
        processing_dialogs = connection.execute(
            "SELECT COUNT(*) FROM max_dialogs WHERE state='processing'"
        ).fetchone()[0]
        processing_versions = connection.execute(
            "SELECT COUNT(*) FROM gallery_versions WHERE status='processing'"
        ).fetchone()[0]
        dialog = connection.execute(
            """SELECT d.state FROM max_dialogs d
               JOIN users u ON u.id=d.user_id WHERE u.id=?""",
            (owner_user_id,),
        ).fetchone()
        latest = rows[-1]

    durations = [int(item["duration_ms"]) for item in attempts if item["duration_ms"]]
    costs = [float(item["estimated_cost_rub"]) for item in attempts if item["estimated_cost_rub"] is not None]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "latest successful owner requests",
        "request_cap": limit,
        "request_count": len(attempts),
        "runtime": {
            "observe_only": settings.max_poll_observe_only,
            "owner_allowlist_configured": bool(settings.max_owner_user_ids),
            "pilot_user_limit": settings.pilot_user_limit,
            "model": settings.openai_image_model,
        },
        "summary": {
            "all_succeeded": all(item["status"] == "succeeded" for item in attempts),
            "average_duration_ms": round(sum(durations) / len(durations)) if durations else None,
            "total_estimated_cost_rub": round(sum(costs), 2),
            "gallery_versions_created": sum(
                1 for item in attempts if item["gallery_version_created"]
            ),
        },
        "attempts": attempts,
        "final_checks": {
            "pending_or_processing_attempts": pending,
            "processing_dialogs": processing_dialogs,
            "processing_gallery_versions": processing_versions,
            "owner_dialog_state": dialog["state"] if dialog else None,
            "sqlite_quick_check": quick_check,
            "orphan_private_file_count": _orphan_count(settings, database),
            "demo_successful_generations": latest["successful_generations"],
            "demo_max_generations": latest["max_generations"],
            "demo_status": latest["session_status"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(build_report(load_settings(), limit=args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
