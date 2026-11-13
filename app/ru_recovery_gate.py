"""Check isolated recovered state and provider evidence before reopening sales."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from app.backup import BackupError, BackupManager
from app.ru_recovery_consistency import reconcile_provider, validate_media, verify_materialized_media


def _read_json(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise BackupError("Invalid private recovery evidence file")
    return json.loads(path.read_bytes())


def gate(root, source_users, state_sha256, incident, evidence, *, checkpoint=None,
         checkpoint_sha256=None, checkpoint_started=None):
    if root.is_symlink() or not root.is_dir():
        raise BackupError("Restore must be an isolated full recovery directory")
    manifest = BackupManager._validate_recovery_manifest(_read_json(root / "manifest.json", 64 * 1024**2))
    hashes = manifest["sha256"]
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != state_sha256:
        raise BackupError("Full recovery identity does not match the independently verified proof")
    base_db = root / "database/photo_bot.sqlite3"
    if base_db.is_symlink() or BackupManager._sha256(base_db) != hashes["database/photo_bot.sqlite3"]:
        raise BackupError("Full recovery SQLite SHA256 mismatch")
    verify_materialized_media(root, hashes)
    full_time = datetime.fromisoformat(manifest["created_at"])
    if incident.tzinfo is None or not full_time <= incident:
        raise BackupError("Invalid incident time")
    selected, snapshot_time = base_db, full_time
    if checkpoint is not None:
        if (checkpoint.is_symlink() or not isinstance(checkpoint_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", checkpoint_sha256)
                or BackupManager._sha256(checkpoint) != checkpoint_sha256
                or checkpoint_started is None or checkpoint_started.tzinfo is None
                or not full_time <= checkpoint_started <= incident):
            raise BackupError("Unverified SQLite checkpoint")
        selected, snapshot_time = checkpoint, checkpoint_started
    elif checkpoint_sha256 is not None or checkpoint_started is not None:
        raise BackupError("Incomplete checkpoint evidence")
    for candidate in {base_db, selected}:
        if any(p.exists() or p.is_symlink() for p in (
            candidate.with_name(candidate.name + suffix) for suffix in ("-wal", "-shm", "-journal")
        )):
            raise BackupError("Restore SQLite must be standalone; unexpected transaction sidecar")
    BackupManager._validate_sqlite(selected)
    media = validate_media(selected, source_users, hashes,
                           previous_db=base_db if checkpoint is not None else None)
    finance = reconcile_provider(selected, evidence, from_time=full_time, through_time=incident)
    return {**media, **finance, "compatibility": "PASS", "publication": "NOT_AUTHORIZED",
            "sqlite_snapshot_age_seconds": int((incident - snapshot_time).total_seconds()),
            "media_snapshot_age_seconds": int((incident - full_time).total_seconds())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-root", required=True, type=Path)
    parser.add_argument("--source-users", required=True, type=Path)
    parser.add_argument("--full-state-sha256", required=True)
    parser.add_argument("--incident", required=True, type=datetime.fromisoformat)
    parser.add_argument("--provider-evidence", required=True, type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--checkpoint-sha256")
    parser.add_argument("--checkpoint-started", type=datetime.fromisoformat)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(gate(args.full_root, args.source_users, args.full_state_sha256,
                              args.incident, _read_json(args.provider_evidence, 8 * 1024**2),
                              checkpoint=args.checkpoint, checkpoint_sha256=args.checkpoint_sha256,
                              checkpoint_started=args.checkpoint_started), sort_keys=True))
        return 0
    except BackupError as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc), "sales": "BLOCKED"}))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print(json.dumps({"status": "FAIL", "reason": "invalid_or_unreadable_evidence",
                          "sales": "BLOCKED"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
