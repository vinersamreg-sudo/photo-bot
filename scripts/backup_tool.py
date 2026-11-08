"""Isolated backup-only CLI: never initializes or starts the Ravuna application."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import dotenv_values
from app.backup import BackupError, BackupManager, _os_reason
from app.backup_daily import DailyRecovery
from app.recovery_retention import NAME, artifact, read_json, validate, RetentionError

SAMARA = timezone(timedelta(hours=4))


def reusable_recovery(manager):
    """Read-only daily/retry selection, preserving distinct recovery points."""
    latest = manager.backup_dir / "latest_recovery.json"
    if latest.exists():
        metadata = read_json(latest)
        name = metadata["recovery_bundle_name"]
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise BackupError("Latest recovery reference is unsafe")
        current = artifact(manager.backup_dir / name)
        proof = read_json(manager.backup_dir / (name + ".proof.json"))
        try:
            validate(proof, name, current)
        except RetentionError:
            if (proof.get("creation_status") != "PASS"
                    or proof.get("sha256") != current["sha256"]
                    or proof.get("size_bytes") != current["size_bytes"]
                    or metadata.get("sha256") != current["sha256"]
                    or proof.get("restore", {}).get("overall") == "FAIL"):
                raise BackupError("Unverified recovery requires operator review") from None
            return metadata
        if (datetime.fromisoformat(metadata['created_at']).astimezone(SAMARA).date()
                == datetime.now(SAMARA).date()
                and metadata.get('source_revision') == manager._source_revision()):
            return metadata
    return None


def prepare_recovery(manager, password):
    manager._require_passphrase(password)
    return reusable_recovery(manager) or manager.create_recovery_bundle(password)


def previous_point(manager, *, daily=False):
    reused = None if daily else reusable_recovery(manager)
    excluded = reused["recovery_bundle_name"] if reused else None
    candidates = []
    for file in manager.backup_dir.glob('ravuna-recovery-*.tar.gz.enc'):
        if file.name == excluded:
            continue
        try:
            current = artifact(file)
            proof = read_json(file.with_name(file.name + '.proof.json'))
            created = validate(proof, file.name, current)
            candidates.append((created, dict(proof['offsite'], backup_name=file.name)))
        except (OSError, RetentionError, ValueError, TypeError):
            continue
    if not candidates:
        raise BackupError('A distinct previous fully verified point is required')
    return max(candidates, key=lambda item: item[0])[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("command", choices=(
        "backup-create", "backup-restore-test", "backup-mark-offsite",
        "recovery-create", "recovery-restore-test", "recovery-mark-offsite",
        "recovery-prune",
        "previous-point", "daily-previous-point",
        "daily-create", "daily-accept", "daily-cleanup",
    ))
    parser.add_argument("--backup")
    parser.add_argument("--confirmed-backup")
    parser.add_argument("--provider")
    parser.add_argument("--restore-root", type=Path)
    parser.add_argument("--keep", type=int, default=2)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report-preserved-legacy", action="store_true")
    parser.add_argument("--passphrase-stdin", action="store_true")
    parser.add_argument("--owner")
    parser.add_argument("--backup-sha")
    parser.add_argument("--recovery-sha")
    args = parser.parse_args(argv)
    try:
        if args.report_preserved_legacy and (
            args.command != "recovery-prune" or not args.apply or args.keep != 2
        ):
            raise BackupError("Legacy warning is only available for two-point scheduled rotation")
        root = args.root.resolve(strict=True)
        values = dotenv_values(root / ".env")
        configured_root = values.get("BASE_DIR")
        if configured_root and Path(configured_root).resolve() != root:
            raise BackupError("Backup application root mismatch")
        directory = Path(values.get("BACKUP_DIR") or "data/backups")
        directory = directory if directory.is_absolute() else root / directory
        if directory.resolve() != root / "data/backups" or directory.is_symlink():
            raise BackupError("Backup directory is outside the approved location")
        database = root / "data/photo_bot.sqlite3"
        if not database.is_file() or database.is_symlink():
            raise BackupError("Existing production SQLite is required")
        days = int(values.get("BACKUP_RETENTION_DAYS") or 14)
        if days <= 0:
            raise BackupError("SQLite backup age must be positive")
        manager = BackupManager(database, directory, days)
        password = ""
        if args.command.endswith("create") or args.command.endswith("restore-test"):
            if not args.passphrase_stdin:
                raise BackupError("Passphrase input must use stdin")
            password = sys.stdin.readline().rstrip("\r\n")
        if args.command in ("backup-restore-test", "recovery-restore-test",
                            "backup-mark-offsite", "recovery-mark-offsite") and not args.backup:
            raise BackupError("Backup name is required")
        if args.command.endswith("mark-offsite") and not args.provider:
            raise BackupError("Off-site provider is required")
        if args.command == "daily-previous-point":
            report = previous_point(manager, daily=True)
        elif args.command.startswith("daily-"):
            cycle = DailyRecovery(manager, args.owner)
            if args.command == "daily-create":
                report = cycle.create(password)
            elif args.command == "daily-accept":
                report = cycle.accept(args.provider, args.backup_sha, args.recovery_sha)
            else:
                report = cycle.cleanup()
        elif args.command == "backup-create":
            report = manager.create(password)
        elif args.command == "backup-restore-test":
            report = manager.restore_test(Path(args.backup), password)
        elif args.command == "recovery-create":
            report = prepare_recovery(manager, password)
        elif args.command == "recovery-restore-test":
            report = manager.restore_recovery_bundle(
                Path(args.backup), password, restore_root=args.restore_root,
            )
        elif args.command == "backup-mark-offsite":
            report = manager.mark_offsite(args.backup, args.provider)
        elif args.command == "recovery-mark-offsite":
            report = manager.mark_recovery_offsite(args.backup, args.provider)
        elif args.command == "previous-point":
            report = previous_point(manager)
        else:
            if not args.confirmed_backup:
                raise BackupError("Confirmed backup is required for retention")
            report = manager.prune_recovery_by_count(
                keep=args.keep, confirmed_backup=args.confirmed_backup, dry_run=not args.apply,
            )
        print(json.dumps(report, sort_keys=True))
        unproven = any(e.get("action") == "PRESERVE_UNKNOWN" for e in report.get("entries", []))
        if unproven and args.report_preserved_legacy and report["successful"] - report["removed"] == 2:
            print("::warning::Legacy unverified recovery preserved outside the two verified points; "
                  "review it separately. Its bytes still count toward the 16 GiB budget.",
                  file=sys.stderr)
            unproven = False
        return 1 if report.get("overall") == "FAIL" or unproven else 0
    except BackupError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}), file=sys.stderr)
    except (OSError, sqlite3.Error, ValueError, RetentionError, KeyError) as exc:
        reason = _os_reason(exc) if isinstance(exc, OSError) else "invalid_backup_state"
        print(json.dumps({"status": "FAIL", "reason": reason,
                          "error_type": type(exc).__name__}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
