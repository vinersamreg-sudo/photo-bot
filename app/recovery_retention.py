"""Fail-closed, hash-bound recovery retention; planning never writes files."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


NAME = re.compile(r"ravuna-recovery-(\d{8}T\d{12}Z)\.tar\.gz\.enc\Z")


class RetentionError(RuntimeError):
    """Privacy-safe error, never including a path or raw OS diagnostic."""


def _identity(value):
    # Windows stat/fstat expose different historical ctime meanings; POSIX ctime
    # detects metadata/replacement races in production. Both retain inode+mtime.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns if os.name == "posix" else 0)


def _open_regular(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RetentionError("Recovery evidence/artifact is not a regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if _identity(os.fstat(descriptor)) != _identity(before):
        os.close(descriptor)
        raise RetentionError("Recovery evidence/artifact changed")
    return descriptor, before


def artifact(path):
    if not NAME.fullmatch(path.name):
        raise RetentionError("Recovery artifact name is invalid")
    descriptor, before = _open_regular(path)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise RetentionError("Recovery artifact changed")
    if _identity(path.lstat()) != _identity(before):
        raise RetentionError("Recovery artifact changed")
    return {"sha256": digest.hexdigest(), "size_bytes": before.st_size,
            "_identity": _identity(before)}


def read_json(path):
    descriptor, before = _open_regular(path)
    with os.fdopen(descriptor, "rb") as stream:
        contents = stream.read(2 * 1024 * 1024 + 1)
    if len(contents) > 2 * 1024 * 1024 or _identity(path.lstat()) != _identity(before):
        raise RetentionError("Recovery evidence is invalid")
    value = json.loads(contents)
    if not isinstance(value, dict):
        raise RetentionError("Recovery evidence is invalid")
    return value


def validate(proof, name, current, *, offsite=True):
    if (proof.get("proof_version") != 1 or proof.get("backup_name") != name
            or proof.get("creation_status") != "PASS"
            or proof.get("sha256") != current["sha256"]
            or proof.get("size_bytes") != current["size_bytes"] or not current["size_bytes"]):
        raise RetentionError("Recovery creation proof is missing or mismatched")
    try:
        created = datetime.fromisoformat(proof["created_at"])
        if created.tzinfo is None:
            raise ValueError
        created = created.astimezone(timezone.utc)
        if created.strftime("%Y%m%dT%H%M%S%fZ") != NAME.fullmatch(name)[1]:
            raise ValueError
        restored = proof["restore"]
        tested = datetime.fromisoformat(restored["tested_at"])
        if (tested.tzinfo is None or tested < created
                or restored.get("overall") != "PASS"
                or restored.get("artifact_status") != "PASS"
                or restored.get("sqlite_status") != "PASS"
                or restored.get("restore_status") != "PASS"
                or restored.get("sha256") != current["sha256"]):
            raise ValueError
        if offsite:
            copied = proof["offsite"]
            copied_at = datetime.fromisoformat(copied["copied_at"])
            if (copied_at.tzinfo is None or copied_at < tested
                    or copied.get("overall") != "PASS"
                    or copied.get("sha256") != current["sha256"]
                    or not copied.get("provider")):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise RetentionError("Recovery restore/off-site proof is missing or mismatched") from None
    return created


def _write_stage(directory, name, stage, value):
    """Private per-artifact receipt, retaining legacy latest-status files too."""
    if directory.is_symlink() or not NAME.fullmatch(name):
        raise RetentionError("Recovery evidence destination is unsafe")
    destination = directory / (name + ".proof.json")
    if destination.is_symlink():
        raise RetentionError("Recovery evidence destination is unsafe")
    proof = read_json(destination) if destination.exists() else {
        "proof_version": 1, "backup_name": name,
    }
    if stage == "creation":
        proof = {"proof_version": 1, "backup_name": name,
                 "creation_status": "PASS", "created_at": value["created_at"],
                 "size_bytes": value["size_bytes"], "sha256": value["sha256"]}
    else:
        proof[stage] = value
    descriptor, temporary = tempfile.mkstemp(prefix=".recovery-proof-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(proof, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def plan(directory, *, confirmed_backup, keep=7, evidence=None):
    """Unknown/partial/link artifacts are protected, never counted as success."""
    if type(keep) is not int or keep < 1:
        raise RetentionError("Recovery keep count must be positive")
    if directory.is_symlink() or not directory.is_dir():
        raise RetentionError("Recovery directory is unsafe")
    supplemental = evidence or {}
    entries = []
    for path in sorted(directory.glob("ravuna-recovery-*.tar.gz.enc")):
        entry = {"backup_name": path.name, "action": "PRESERVE_UNKNOWN", "size_bytes": 0}
        try:
            current = artifact(path)
            entry["size_bytes"] = current["size_bytes"]
            receipt = directory / (path.name + ".proof.json")
            # Never override an existing failed/malformed/symlink receipt.
            proof = read_json(receipt) if receipt.exists() or receipt.is_symlink() else supplemental.get(path.name, {})
            created = validate(proof, path.name, current)
            entry.update(created_at=created.isoformat(), sha256=current["sha256"],
                         _identity=current["_identity"],
                         _proof_digest=hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest(),
                         action="KEEP")
        except (RetentionError, OSError, ValueError, TypeError):
            entry["reason"] = "unproven_or_unsafe"
        entries.append(entry)
    successful = sorted((entry for entry in entries if entry["action"] == "KEEP"),
                        key=lambda item: (item["created_at"], item["backup_name"]), reverse=True)
    if not successful or successful[0]["backup_name"] != confirmed_backup:
        raise RetentionError("Newest recovery is not fully confirmed; no deletion allowed")
    if any(NAME.fullmatch(entry["backup_name"]) and entry["backup_name"] > confirmed_backup
           for entry in entries):
        raise RetentionError("A newer recovery is unconfirmed; no deletion allowed")
    for entry in successful[keep:]:
        entry["action"] = "DELETE"
    return {"keep": keep, "confirmed_backup": confirmed_backup, "entries": entries,
            "successful": len(successful),
            "delete_bytes": sum(entry["size_bytes"] for entry in entries if entry["action"] == "DELETE")}


@contextmanager
def _lock(directory):
    # All cooperating prune processes serialize; no lock is created by dry-run.
    path = directory / ".recovery-retention.lock"
    if path.is_symlink():
        raise RetentionError("Recovery retention lock is unsafe")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RetentionError("Recovery retention lock is unsafe")
        if os.name == "posix":
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        else:
            import msvcrt
            if not path.stat().st_size:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def prune(directory, *, confirmed_backup, keep=7, dry_run=True, evidence=None):
    def prepare():
        return plan(directory, confirmed_backup=confirmed_backup, keep=keep, evidence=evidence)
    if dry_run:
        report = prepare()
    else:
        if directory.is_symlink() or not directory.is_dir():
            raise RetentionError("Recovery directory is unsafe")
        with _lock(directory):
            report = prepare()
            # Revalidate the complete deletion set before the first unlink.
            for entry in report["entries"]:
                if entry["action"] in {"KEEP", "DELETE"}:
                    if _identity((directory / entry["backup_name"]).lstat()) != tuple(entry["_identity"]):
                        raise RetentionError("Recovery artifact changed; no deletion allowed")
                    receipt = directory / (entry["backup_name"] + ".proof.json")
                    proof = read_json(receipt) if receipt.exists() or receipt.is_symlink() else (evidence or {}).get(entry["backup_name"], {})
                    digest = hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
                    if digest != entry["_proof_digest"]:
                        raise RetentionError("Recovery evidence changed; no deletion allowed")
            for entry in report["entries"]:
                if entry["action"] == "DELETE":
                    path = directory / entry["backup_name"]
                    if _identity(path.lstat()) != tuple(entry["_identity"]):
                        raise RetentionError("Recovery artifact changed; deletion stopped")
                    path.unlink()
    report["dry_run"] = dry_run
    report["removed"] = 0 if dry_run else sum(e["action"] == "DELETE" for e in report["entries"])
    for entry in report["entries"]:
        entry.pop("_identity", None)
        entry.pop("_proof_digest", None)
    return report


def write_stage(directory, name, stage, value):
    if directory.is_symlink() or not directory.is_dir():
        raise RetentionError("Recovery evidence destination is unsafe")
    with _lock(directory):
        _write_stage(directory, name, stage, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only recovery retention plan unless --apply is explicit")
    parser.add_argument("--confirmed-backup", required=True)
    parser.add_argument("--keep", type=int, default=7)
    parser.add_argument("--evidence", type=Path, help="Trusted operator-reviewed legacy per-artifact proofs")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        from app.config import load_settings
        settings = load_settings()
        evidence = read_json(args.evidence) if args.evidence else None
        report = prune(settings.backup_dir_path, confirmed_backup=args.confirmed_backup,
                       keep=args.keep, dry_run=not args.apply, evidence=evidence)
    except (RetentionError, OSError, ValueError, TypeError):
        print(json.dumps({"status": "BLOCKED", "reason": "unproven_or_unsafe",
                          "removed": "NOT MEASURABLE" if args.apply else 0}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
