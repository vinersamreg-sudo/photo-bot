"""Count retention for verified encrypted backup artifacts; dry-run by default.

The workflow supplies two DISTINCT recovery points only after readback hashes
match their VPS receipts. Unknown/failed-run artifacts are preserved, not trusted
by their names. This never targets CI artifacts from other workflows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from datetime import datetime

NAME = re.compile(r"ravuna-production-backup-(\d+)\Z")


class PruneBlocked(RuntimeError):
    pass


def verify_readback(directory, name, expected_sha):
    """Read ciphertext only; never decrypt, publish proofs or modify artifacts."""
    if (not re.fullmatch(r'ravuna-recovery-\d{8}T\d{12}Z\.tar\.gz\.enc', name)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_sha)
            or directory.is_symlink()):
        raise PruneBlocked('Readback metadata is invalid')
    candidate = directory / name
    started = time.monotonic()
    before = candidate.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise PruneBlocked('Readback ciphertext is unsafe')
    def file_identity(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    digest = hashlib.sha256()
    with os.fdopen(descriptor, 'rb') as stream:
        if file_identity(os.fstat(stream.fileno())) != file_identity(before):
            raise PruneBlocked('Readback ciphertext changed')
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
        if file_identity(os.fstat(stream.fileno())) != file_identity(before):
            raise PruneBlocked('Readback ciphertext changed')
    if (file_identity(candidate.lstat()) != file_identity(before)
            or not before.st_size or digest.hexdigest() != expected_sha):
        raise PruneBlocked('Readback ciphertext checksum mismatch')
    return {'overall': 'PASS', 'stage': 'full_offsite_readback',
            'bytes': before.st_size, 'sha256': digest.hexdigest(),
            'hash_seconds': round(time.monotonic() - started, 3)}


def api(repository, path, method="GET"):
    result = subprocess.run(
        ["gh", "api", "--method", method, f"repos/{repository}/{path}"],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise PruneBlocked("GitHub backup artifact operation failed")
    return json.loads(result.stdout) if result.stdout.strip() else None


def identity(value):
    return tuple(value.get(k) for k in ("id", "name", "size_in_bytes", "created_at", "digest", "expired"))


def resolve_previous(repository, provider, backup_name=None):
    match = re.fullmatch(r"github-actions:(\d+):(\d+)", provider)
    if match:
        run_id, artifact_id = map(int, match.groups())
        artifact = api(repository, f"actions/artifacts/{artifact_id}")
    elif provider == "github-actions":
        # One-time compatibility: the subsequent exact member/hash readback is
        # mandatory, so a wrong legacy association fails before any pruning.
        runs = api(repository, "actions/workflows/backup.yml/runs?status=success&per_page=100")["workflow_runs"]
        if backup_name:
            name = re.fullmatch(r'ravuna-recovery-(\d{8}T\d{12}Z)\.tar\.gz\.enc', backup_name)
            if not name:
                raise PruneBlocked('Previous recovery name is invalid')
            from datetime import timezone
            created = datetime.strptime(name[1], '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
            runs = [r for r in runs if datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')) <= created
                    <= datetime.fromisoformat(r['updated_at'].replace('Z', '+00:00'))]
        if len(runs) != 1:
            raise PruneBlocked('Legacy off-site run is ambiguous')
        if not runs:
            raise PruneBlocked("Legacy off-site run cannot be established")
        run_id = runs[0]["id"]
        artifacts = api(repository, f"actions/runs/{run_id}/artifacts")["artifacts"]
        matching = [a for a in artifacts if a["name"] == f"ravuna-production-backup-{run_id}"]
        if len(matching) != 1:
            raise PruneBlocked("Previous backup artifact is not unique")
        artifact = matching[0]
    else:
        raise PruneBlocked("Previous off-site provider is unknown")
    if artifact.get("expired") or not artifact.get("size_in_bytes"):
        raise PruneBlocked("Previous off-site artifact is unavailable")
    if artifact["name"] != f"ravuna-production-backup-{run_id}":
        raise PruneBlocked("Previous artifact provenance mismatch")
    return {"run_id": run_id, "artifact_id": artifact["id"], "name": artifact["name"]}


def plan(artifacts, keep_ids, get_run, held_ids=()):
    if len(keep_ids) != 2 or len(set(keep_ids)) != 2:
        raise PruneBlocked("Two distinct verified off-site artifacts are required")
    indexed = {a["id"]: a for a in artifacts}
    selected = [indexed.get(i) for i in keep_ids]
    if any(not a or a.get("expired") or not a.get("size_in_bytes") for a in selected):
        raise PruneBlocked("A protected off-site artifact is missing or expired")
    for item in selected:
        match = NAME.fullmatch(item.get("name", ""))
        if not match or int(match[1]) != item.get("workflow_run", {}).get("id"):
            raise PruneBlocked("Protected artifact provenance mismatch")
        run = get_run(int(match[1]))
        if (run.get("path") != ".github/workflows/backup.yml"
                or run.get("event") not in ("schedule", "workflow_dispatch")):
            raise PruneBlocked("Protected artifact belongs to a different workflow")
    cutoff = max(datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")) for a in selected)
    entries = []
    for item in artifacts:
        match = NAME.fullmatch(item.get("name", ""))
        if not match:
            continue
        action = "KEEP" if item["id"] in keep_ids else "PRESERVE_UNKNOWN"
        if item["id"] in held_ids:
            action = "PRESERVE_HOLD"
        elif item.get("expired") and action != "KEEP":
            action = "EXPIRED"
        elif action != "KEEP" and int(match[1]) == item.get("workflow_run", {}).get("id"):
            created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            if created < cutoff:
                run = get_run(int(match[1]))
                if (run.get("path") == ".github/workflows/backup.yml"
                        and run.get("event") in ("schedule", "workflow_dispatch")
                        and run.get("conclusion") == "success"):
                    action = "DELETE"
        entries.append({"id": item["id"], "action": action,
                        "bytes": item.get("size_in_bytes", 0), "identity": identity(item)})
    return entries


def execute(repository, keep_ids, *, apply=False, held_ids=()):
    artifacts, page = [], 1
    while True:
        batch = api(repository, f"actions/artifacts?per_page=100&page={page}")["artifacts"]
        artifacts.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    entries = plan(artifacts, keep_ids,
                   lambda run: api(repository, f"actions/runs/{run}"), held_ids)
    removed = 0
    if apply:
        # Recheck the entire protected/deletion set before the first deletion.
        for entry in entries:
            if entry["action"] in ("KEEP", "DELETE", "PRESERVE_HOLD"):
                current = api(repository, f"actions/artifacts/{entry['id']}")
                if identity(current) != entry["identity"]:
                    raise PruneBlocked("Artifact changed; deletion blocked")
        for entry in entries:
            if entry["action"] == "DELETE":
                api(repository, f"actions/artifacts/{entry['id']}", "DELETE")
                removed += 1
    for entry in entries:
        entry.pop("identity")
    return {"dry_run": not apply, "removed": removed, "entries": entries,
            "delete_bytes": sum(e["bytes"] for e in entries if e["action"] == "DELETE"),
            "unknown": sum(e["action"] == "PRESERVE_UNKNOWN" for e in entries)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--keep-artifact-id", action="append", type=int, required=True)
    parser.add_argument("--held-artifact-id", action="append", type=int, default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = execute(args.repository, args.keep_artifact_id,
                         apply=args.apply, held_ids=args.held_artifact_id)
        print(json.dumps(report, sort_keys=True))
        return 1 if report["unknown"] else 0
    except (PruneBlocked, KeyError, ValueError, TypeError):
        print(json.dumps({"status": "BLOCKED", "reason": "unproven_or_unsafe"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
