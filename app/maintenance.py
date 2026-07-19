"""Safe retention and orphan cleanup with an auditable dry-run."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import Database
from app.gallery import GalleryService
from app.storage import PrivateStorage
from app.openai_client import create_openai_client
from app.provider_context import OpenAIProviderContextGateway, ProviderContextService


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _referenced_private_files(database: Database) -> set[Path]:
    queries = (
        ("demo_sessions", ("source_file_path",)),
        ("generation_attempts", ("source_path", "original_result_path", "demo_result_path")),
        ("gallery_items", ("original_source_path", "cover_preview_path", "preview_small_path", "preview_large_path")),
        ("gallery_versions", ("source_path", "preview_watermarked_path", "original_path")),
    )
    paths: set[Path] = set()
    with database.read() as connection:
        for table, columns in queries:
            rows = connection.execute(
                f"SELECT {','.join(columns)} FROM {table}"
            ).fetchall()
            for row in rows:
                for column in columns:
                    if row[column]:
                        paths.add(Path(row[column]).resolve())
                if table == "demo_sessions" and row["source_file_path"]:
                    session_root = Path(row["source_file_path"]).resolve().parent.parent
                    paths.add((session_root / "metadata.json").resolve())
    return paths


def run_maintenance(settings: Settings, *, execute: bool) -> dict[str, Any]:
    database = Database(settings.database_path)
    storage = PrivateStorage(
        settings.users_dir, settings.max_source_file_size_mb * 1024 * 1024
    )
    gateway = (
        OpenAIProviderContextGateway(create_openai_client(settings))
        if settings.openai_api_key
        else None
    )
    provider_contexts = ProviderContextService(settings, database, gateway)
    context_due = provider_contexts.cleanup_due(execute=execute)
    gallery = GalleryService(
        database,
        storage,
        settings,
        provider_context_service=provider_contexts,
    )
    due = gallery.purge_due(execute=execute)
    now = time.time()
    temp_cutoff = now - settings.cleanup_temp_retention_hours * 3600
    orphan_cutoff = now - settings.cleanup_orphan_grace_hours * 3600
    referenced = _referenced_private_files(database)
    temp_candidates: list[Path] = []
    orphan_candidates: list[Path] = []
    for candidate in settings.temp_dir.iterdir() if settings.temp_dir.is_dir() else ():
        if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_mtime < temp_cutoff:
            temp_candidates.append(candidate)
    if settings.users_dir.is_dir():
        for candidate in settings.users_dir.rglob("*"):
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and _inside(settings.users_dir, candidate)
                and candidate.resolve() not in referenced
                and candidate.stat().st_mtime < orphan_cutoff
            ):
                orphan_candidates.append(candidate)
    candidates = (*temp_candidates, *orphan_candidates)
    bytes_total = sum(path.stat().st_size for path in candidates)
    removed = 0
    if execute:
        for candidate in candidates:
            candidate.unlink(missing_ok=True)
            removed += 1
        for directory in sorted(
            (path for path in settings.users_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ) if settings.users_dir.is_dir() else ():
            try:
                directory.rmdir()
            except OSError:
                pass
    report = {
        "mode": "execute" if execute else "dry-run",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "gallery_due_count": len(due),
        "provider_context_due_count": len(context_due),
        "temp_candidate_count": len(temp_candidates),
        "orphan_candidate_count": len(orphan_candidates),
        "remaining_orphan_count": 0 if execute else len(orphan_candidates),
        "remaining_temp_count": 0 if execute else len(temp_candidates),
        "candidate_bytes": bytes_total,
        "removed_file_count": removed,
    }
    report_path = settings.data_dir / "maintenance_last.json"
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    temporary.replace(report_path)
    return report
