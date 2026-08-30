"""Safe retention and orphan cleanup with an auditable dry-run."""

from __future__ import annotations

import json
import os
import shutil
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
        ("demo_sessions", ("source_file_path", "secondary_source_file_path")),
        (
            "generation_attempts",
            (
                "source_path",
                "secondary_source_path",
                "original_result_path",
                "demo_result_path",
            ),
        ),
        (
            "gallery_items",
            (
                "original_source_path",
                "secondary_source_path",
                "cover_preview_path",
                "preview_small_path",
                "preview_large_path",
            ),
        ),
        (
            "gallery_versions",
            (
                "source_path",
                "secondary_source_path",
                "preview_watermarked_path",
                "original_path",
            ),
        ),
        ("pending_edit_requests", ("primary_source_path", "secondary_source_path")),
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


def _tree_stats(root: Path, allowed_root: Path) -> tuple[int, int, float, int]:
    """Return metadata-only file statistics without following symlinks."""

    if root.is_symlink():
        return 0, 0, 0.0, 1
    try:
        resolved = root.resolve()
        allowed = allowed_root.resolve()
    except OSError:
        return 0, 0, 0.0, 1
    if allowed not in resolved.parents:
        return 0, 0, 0.0, 1
    if not root.exists():
        return 0, 0, 0.0, 0
    if not root.is_dir():
        return 0, 0, 0.0, 1

    file_count = 0
    byte_count = 0
    latest_mtime = root.stat().st_mtime
    anomaly_count = 0
    for directory, child_dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in (*child_dirs, *files):
            candidate = parent / name
            if candidate.is_symlink():
                anomaly_count += 1
                continue
            try:
                metadata = candidate.stat()
            except OSError:
                anomaly_count += 1
                continue
            latest_mtime = max(latest_mtime, metadata.st_mtime)
            if candidate.is_file():
                file_count += 1
                byte_count += metadata.st_size
    return file_count, byte_count, latest_mtime, anomaly_count


def _write_report(settings: Settings, report: dict[str, Any]) -> None:
    report_path = settings.data_dir / "maintenance_last.json"
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(report_path)


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
    gallery = GalleryService(
        database,
        storage,
        settings,
        provider_context_service=provider_contexts,
    )
    due = gallery.due_for_cleanup()
    gallery_file_count = 0
    gallery_bytes = 0
    path_anomaly_count = 0
    if due:
        placeholders = ",".join("?" for _ in due)
        with database.read() as connection:
            gallery_roots = connection.execute(
                f"SELECT storage_root_path FROM gallery_items WHERE id IN ({placeholders})",
                due,
            ).fetchall()
            session_roots = connection.execute(
                f"SELECT source_file_path FROM demo_sessions WHERE gallery_item_id IN ({placeholders})",
                due,
            ).fetchall()
        roots = {Path(row["storage_root_path"]) for row in gallery_roots}
        roots.update(
            Path(row["source_file_path"]).parent.parent
            for row in session_roots
            if row["source_file_path"]
        )
        for root in roots:
            files, size, _, anomalies = _tree_stats(
                root, settings.users_dir
            )
            gallery_file_count += files
            gallery_bytes += size
            path_anomaly_count += anomalies

    now = time.time()
    temp_cutoff = now - settings.cleanup_temp_retention_hours * 3600
    orphan_cutoff = now - settings.cleanup_orphan_grace_hours * 3600
    referenced = _referenced_private_files(database)
    temp_candidates: list[Path] = []
    temp_file_count = 0
    temp_bytes = 0
    orphan_candidates: list[Path] = []
    for candidate in settings.temp_dir.iterdir() if settings.temp_dir.is_dir() else ():
        if candidate.is_symlink():
            path_anomaly_count += 1
            continue
        if candidate.is_file():
            metadata = candidate.stat()
            if metadata.st_mtime >= temp_cutoff:
                continue
            temp_candidates.append(candidate)
            temp_file_count += 1
            temp_bytes += metadata.st_size
        elif candidate.is_dir():
            files, size, latest_mtime, anomalies = _tree_stats(candidate, settings.temp_dir)
            path_anomaly_count += anomalies
            if not anomalies and latest_mtime < temp_cutoff:
                temp_candidates.append(candidate)
                temp_file_count += files
                temp_bytes += size
    if settings.users_dir.is_dir():
        for candidate in settings.users_dir.rglob("*"):
            if candidate.is_symlink():
                path_anomaly_count += 1
                continue
            if (
                candidate.is_file()
                and _inside(settings.users_dir, candidate)
                and candidate.resolve() not in referenced
                and candidate.stat().st_mtime < orphan_cutoff
            ):
                orphan_candidates.append(candidate)

    orphan_bytes = sum(path.stat().st_size for path in orphan_candidates)
    candidate_bytes = gallery_bytes + temp_bytes + orphan_bytes
    context_due = provider_contexts.cleanup_due(execute=False)
    report = {
        "mode": "execute" if execute else "dry-run",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "gallery_due_count": len(due),
        "gallery_candidate_file_count": gallery_file_count,
        "gallery_candidate_bytes": gallery_bytes,
        "provider_context_due_count": len(context_due),
        "temp_candidate_count": len(temp_candidates),
        "temp_candidate_file_count": temp_file_count,
        "temp_candidate_bytes": temp_bytes,
        "orphan_candidate_count": len(orphan_candidates),
        "orphan_candidate_bytes": orphan_bytes,
        "path_anomaly_count": path_anomaly_count,
        "remaining_gallery_count": len(due),
        "remaining_orphan_count": len(orphan_candidates),
        "remaining_temp_count": len(temp_candidates),
        "candidate_bytes": candidate_bytes,
        "removed_gallery_count": 0,
        "removed_temp_entry_count": 0,
        "removed_orphan_file_count": 0,
        "removed_file_count": 0,
        "removed_bytes": 0,
    }
    if path_anomaly_count:
        if execute:
            report["mode"] = "refused"
        _write_report(settings, report)
        if execute:
            raise RuntimeError("Retention cleanup refused because a path anomaly was detected")
        return report

    if execute:
        provider_contexts.cleanup_due(execute=True)
        purged = gallery.purge_due(execute=True)
        for candidate in temp_candidates:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink(missing_ok=True)
        for candidate in orphan_candidates:
            candidate.unlink(missing_ok=True)
        for directory in sorted(
            (path for path in settings.users_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ) if settings.users_dir.is_dir() else ():
            try:
                directory.rmdir()
            except OSError:
                pass
        report.update(
            {
                "remaining_gallery_count": 0,
                "remaining_orphan_count": 0,
                "remaining_temp_count": 0,
                "removed_gallery_count": len(purged),
                "removed_temp_entry_count": len(temp_candidates),
                "removed_orphan_file_count": len(orphan_candidates),
                "removed_file_count": gallery_file_count
                + temp_file_count
                + len(orphan_candidates),
                "removed_bytes": candidate_bytes,
            }
        )
    _write_report(settings, report)
    return report
