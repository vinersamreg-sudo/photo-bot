"""Bounded online SQLite recovery checkpoints, independent of the daily media catalog."""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.backup import BackupError, BackupManager
from app.database import CURRENT_SCHEMA_VERSION
from app.max_transport import SingleInstanceLock
from app.recovery_retention import regular_file_identity
from app.ru_recovery import S3, _read_point, _state, bounded_scratch
from app.backup_daily import _ciphertext_cap
from app.ru_recovery_publication import control, object_key, recover_interrupted_writer, reserve
from app.ru_recovery_consistency import validate_finance


CAP = 128 * 1024**2


def _points(catalog):
    if (not isinstance(catalog, dict) or set(catalog) != {"version", "points"}
            or catalog["version"] != 2 or not isinstance(catalog["points"], list)
            or len(catalog["points"]) > 2):
        raise BackupError("Invalid SQLite checkpoint catalog")
    slots = set()
    previous = None
    for point in catalog["points"]:
        if (not isinstance(point, dict) or set(point) != {"slot", "size", "sha256", "started_at"}
                or not object_key(point["slot"]) or point["slot"] in slots
                or not re.fullmatch(r"[a-f0-9]{64}", point["sha256"])
                or type(point["size"]) is not int or not 0 < point["size"] <= CAP):
            raise BackupError("Invalid SQLite checkpoint")
        started = datetime.fromisoformat(point["started_at"])
        if started.tzinfo is None or (previous and started >= previous):
            raise BackupError("Invalid SQLite checkpoint chronology")
        previous = started
        slots.add(point["slot"])
    return catalog["points"]


def publish(store, work, archive, started_at, verify):
    with reserve(store, work, _points) as txn:
        return _publish_reserved(store, work, archive, started_at, verify, txn)


def _publish_reserved(store, work, archive, started_at, verify, txn):
    points = _points(txn.current)
    _validate_interval(points, started_at)
    slot = txn.key
    if archive.stat().st_size > CAP:
        raise BackupError("SQLite checkpoint exceeds its size budget")
    evidence = regular_file_identity(archive)
    point = {"slot": slot, "size": evidence["size_bytes"], "sha256": evidence["sha256"],
             "started_at": started_at.isoformat()}
    updated = {"version": 2, "points": [point, *points[:1]]}
    _points(updated)
    store.put(slot, archive)
    readback = work / "checkpoint.readback.enc"
    _read_point(store, point, readback, regular_file_identity)
    verify(readback)
    txn.publish(updated)
    return {"checkpoints": len(updated["points"]), "checkpoint_started_at": point["started_at"]}


def _validate_interval(points, started_at):
    if started_at.tzinfo is None or (points and int(started_at.timestamp()) // 900 <=
            int(datetime.fromisoformat(points[0]["started_at"]).timestamp()) // 900):
        raise BackupError("SQLite checkpoint already exists for this interval")


def _prove(archive, target, phrase):
    BackupManager._openssl("-d", "-in", str(archive), "-out", str(target), passphrase=phrase)
    status = BackupManager._validate_sqlite(target)
    if not 16 <= status["migration"] <= CURRENT_SCHEMA_VERSION:
        raise BackupError("Unsupported SQLite checkpoint schema")
    validate_finance(target)
    target.chmod(0o600)


def restore_latest(store, work, destination, phrase):
    catalog, _etag = store.get_catalog(work)
    points = _points(control(catalog, _points))
    if not points:
        raise BackupError("No verified SQLite checkpoint")
    readback = work / "checkpoint.readback.enc"
    _read_point(store, points[0], readback, regular_file_identity)
    staged = work / "restored.sqlite3"
    _prove(readback, staged, phrase)
    # A hard link is atomic and fails if the destination appeared; never
    # replace an existing SQLite file or write inside the live runtime.
    destination.hardlink_to(staged)
    return {"checkpoint_started_at": points[0]["started_at"], "sqlite_only": "PASS",
            "sqlite_sha256": regular_file_identity(staged)["sha256"],
            "publication": "NOT_AUTHORIZED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "restore", "freshness", "recover-writer"))
    parser.add_argument("--writers-fenced", action="store_true")
    parser.add_argument("--interrupted-key")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--passphrase-file", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)
    try:
        root, scratch = args.root.absolute(), args.scratch.absolute()
        database = _state(root, scratch, existing=args.command == "create")
        store = S3(args.endpoint, args.bucket, args.region, checkpoints=True)
        with SingleInstanceLock(scratch / "ru-db-recovery.lock"):
            store.ensure_bounded()
            with bounded_scratch(scratch, "db-run") as work:
                now = datetime.now(timezone.utc)
                if args.command == "recover-writer":
                    if not args.writers_fenced:
                        raise BackupError("All writers must be fenced before clearing an interrupted backup")
                    recover_interrupted_writer(store, work, _points, args.interrupted_key)
                    result = {"interrupted_writer_cleared": "PASS"}
                elif args.command == "freshness":
                    points = _points(control(store.get_catalog(work)[0], _points))
                    if len(points) != 2 or not timedelta() <= now - datetime.fromisoformat(
                        points[0]["started_at"]
                    ) <= timedelta(minutes=30):
                        raise BackupError("SQLite checkpoint is stale")
                    result = {"checkpoints": len(points), "freshness": "PASS"}
                else:
                    if not args.passphrase_file:
                        raise BackupError("Passphrase file is required")
                    phrase = args.passphrase_file.read_text(encoding="utf-8").rstrip("\r\n")
                    BackupManager._require_passphrase(phrase)
                    if args.command == "restore":
                        destination = args.destination
                        if (not destination or destination.parent.resolve() != scratch.resolve()
                                or destination.is_symlink() or destination.exists()):
                            raise BackupError("Restore destination must be new and isolated")
                        result = restore_latest(store, work, destination, phrase)
                    else:
                        manager = BackupManager(database, work / "archive", 14)
                        if database.stat().st_size > CAP:
                            raise BackupError("SQLite checkpoint exceeds its size budget")
                        with reserve(store, work, _points) as txn:
                            _validate_interval(_points(txn.current), now)
                            with _ciphertext_cap(CAP):
                                metadata = manager.create(phrase, _standalone_sqlite=True)
                            result = _publish_reserved(
                                store, work, manager.backup_dir / metadata["backup_name"], now,
                                lambda path: _prove(path, work / "checked.sqlite3", phrase), txn)
        print(" ".join(f"{key}={value}" for key, value in result.items()))
        return 0
    except (BackupError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error,
            subprocess.SubprocessError) as exc:
        print(f"status=FAIL error_type={type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
