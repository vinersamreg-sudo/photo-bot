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
    if (not isinstance(proof, dict) or proof.get("proof_version") != 1 or proof.get("backup_name") != name
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
        if not isinstance(restored, dict):
            raise ValueError
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
            if not isinstance(copied, dict):
                raise ValueError
            copied_at = datetime.fromisoformat(copied["copied_at"])
            if (copied_at.tzinfo is None or copied_at < tested
                    or copied.get("overall") != "PASS"
                    or copied.get("sha256") != current["sha256"]
                    or not isinstance(copied.get("provider"), str) or not copied["provider"]):
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
    _publish_proof(destination, proof, replace=True)


def _publish_proof(destination, proof, *, replace):
    descriptor, temporary = tempfile.mkstemp(prefix=".recovery-proof-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(proof, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, destination)
        else:
            # Atomic no-clobber publication: never overwrite a raced-in receipt.
            os.link(temporary, destination, follow_symlinks=False)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if os.name == "posix":
        descriptor = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


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


def import_legacy_proofs(directory, *, evidence, dry_run=True):
    """Explicit, receipt-only migration. No artifact is deleted or rewritten."""
    if directory.is_symlink() or not directory.is_dir() or not isinstance(evidence, dict) or not evidence:
        raise RetentionError("Legacy recovery evidence is missing or unsafe")
    if any(not isinstance(name, str) or not NAME.fullmatch(name) for name in evidence):
        raise RetentionError("Legacy recovery evidence is missing or unsafe")
    if not dry_run and os.name == "posix" and os.geteuid() != directory.stat().st_uid:
        raise RetentionError("Run legacy import as the recovery directory owner")

    def prepare():
        prepared = []
        # Validate the complete requested batch before any receipt publication.
        for name in sorted(evidence):
            current = artifact(directory / name)
            supplied = evidence[name]
            validate(supplied, name, current)
            # Persist only canonical gate metadata, not arbitrary external data.
            proof = {key: supplied[key] for key in (
                "proof_version", "backup_name", "creation_status", "created_at", "size_bytes", "sha256")}
            proof["restore"] = {key: supplied["restore"][key] for key in (
                "overall", "artifact_status", "sqlite_status", "restore_status", "sha256", "tested_at")}
            proof["offsite"] = {key: supplied["offsite"][key] for key in (
                "overall", "provider", "sha256", "copied_at")}
            receipt = directory / (name + ".proof.json")
            existing = None
            if receipt.exists() or receipt.is_symlink():
                existing = read_json(receipt)
                validate(existing, name, current)
            prepared.append((name, current, proof, existing))
        return prepared

    imported = 0
    if dry_run:
        prepared = prepare()
    else:
        with _lock(directory):
            prepared = prepare()
            for name, current, _, existing in prepared:
                if _identity((directory / name).lstat()) != current["_identity"]:
                    raise RetentionError("Legacy artifact changed; import blocked")
                receipt = directory / (name + ".proof.json")
                if existing is None:
                    if receipt.exists() or receipt.is_symlink():
                        raise RetentionError("Legacy receipt changed; import blocked")
                elif read_json(receipt) != existing:
                    raise RetentionError("Legacy receipt changed; import blocked")
            for name, current, proof, existing in prepared:
                if existing is None:
                    if _identity((directory / name).lstat()) != current["_identity"]:
                        raise RetentionError("Legacy artifact changed; import stopped")
                    _publish_proof(directory / (name + ".proof.json"), proof, replace=False)
                    imported += 1
    return {"operation": "import_legacy_proofs", "dry_run": dry_run,
            "would_import": sum(existing is None for _, _, _, existing in prepared),
            "imported": imported, "deleted": 0,
            "entries": [{"backup_name": name,
                         "action": "IMPORT" if existing is None else "ALREADY_CONFIRMED"}
                        for name, _, _, existing in prepared]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only recovery retention plan unless --apply is explicit")
    parser.add_argument("--import-legacy", action="store_true", help="Explicit receipt-only migration")
    parser.add_argument("--confirmed-backup")
    parser.add_argument("--keep", type=int, default=7)
    parser.add_argument("--evidence", type=Path, help="Trusted operator-reviewed legacy per-artifact proofs")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.import_legacy:
        if not args.evidence or args.confirmed_backup or args.keep != 7:
            parser.error("Legacy import requires --evidence; it does not prune")
    elif not args.confirmed_backup:
        parser.error("Pruning requires --confirmed-backup")
    try:
        from app.config import load_settings
        settings = load_settings()
        evidence = read_json(args.evidence) if args.evidence else None
        if args.import_legacy:
            report = import_legacy_proofs(settings.backup_dir_path, evidence=evidence, dry_run=not args.apply)
        else:
            report = prune(settings.backup_dir_path, confirmed_backup=args.confirmed_backup,
                           keep=args.keep, dry_run=not args.apply, evidence=evidence)
    except (RetentionError, OSError, ValueError, TypeError):
        failure = {"status": "BLOCKED", "reason": "unproven_or_unsafe"}
        if args.import_legacy:
            failure.update(operation="import_legacy_proofs", deleted=0,
                           imported="NOT MEASURABLE" if args.apply else 0)
        else:
            failure["removed"] = "NOT MEASURABLE" if args.apply else 0
        print(json.dumps(failure))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
