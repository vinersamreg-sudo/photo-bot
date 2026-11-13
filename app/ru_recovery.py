"""RU recovery, separate from the deployed backup workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.backup import BackupError, BackupManager
from app.backup_daily import _ciphertext_cap
from app.database import CURRENT_SCHEMA_VERSION, ReadOnlyDatabase
from app.max_transport import SingleInstanceLock
from app.media_guard import media_guard
from app.recovery_retention import artifact
from app.ru_recovery_publication import (
    UncertainMultipart, control, object_key, recover_interrupted_writer, reserve,
)
from app.ru_recovery_consistency import validate_finance, validate_media

SAMARA = timezone(timedelta(hours=4))
DAILY_POINTS = 4
WEEKLY_POINTS = 2
MIN_POINTS = 2
MIN_FREE = 16 * 1024**3
CAP = 8 * 1024**3
EXPANDED_CAP = 6 * 1024**3
DB_REL = "data/photo_bot.sqlite3"
READBACK = "ravuna-recovery-20000101T000000000000Z.tar.gz.enc"


def _utcnow():
    return datetime.now(timezone.utc)


def _proof(root: Path, expected_hash: str):
    database = root / "database/photo_bot.sqlite3"
    validate_finance(database)
    with ReadOnlyDatabase(database).read() as conn:
        counts = {name: int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
                  for name in ("users", "gallery_items", "gallery_versions", "payment_orders",
                               "continuation_pack_grants", "credit_ledger", "referral_bonus_grants",
                               "legal_consents", "max_legal_acceptances")}
        if conn.execute("""SELECT 1 FROM user_credit_accounts
                           WHERE available_generation_credits<0 OR reserved_generation_credits<0""").fetchone():
            raise BackupError("Restored credit balance is negative")
    return {"sha256": expected_hash, "counts": counts, "payments": "PASS"}


def _restore(manager, filename, passphrase, dest, expected_hash):
    if artifact(manager.backup_dir / filename)["sha256"] != expected_hash:
        raise BackupError("Ciphertext hash mismatch")
    report = manager.restore_recovery_bundle(Path(filename), passphrase, restore_root=dest,
                                             _expanded_byte_cap=EXPANDED_CAP)
    if report.get("overall") != "PASS" or not 16 <= (report.get("migration") or 0) <= CURRENT_SCHEMA_VERSION:
        raise BackupError("Restore or expected schema verification failed")
    encoded = json.dumps(report["commerce_fingerprints"], sort_keys=True).encode()
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    media = validate_media(dest / "database/photo_bot.sqlite3", manager.users_dir, manifest["sha256"])
    state = json.dumps(manifest["sha256"], sort_keys=True, separators=(",", ":")).encode()
    return {**_proof(dest, expected_hash), "commerce_sha256": hashlib.sha256(encoded).hexdigest(),
            "state_sha256": hashlib.sha256(state).hexdigest(), "source_schema": report["migration"],
            **media}


def _state(root, scratch, *, existing=True):
    actual_root, actual_scratch = root.resolve(), scratch.resolve()
    if (actual_root == actual_scratch or actual_root in actual_scratch.parents
            or actual_scratch in actual_root.parents or root.is_symlink()
            or scratch.is_symlink() or not scratch.is_dir()):
        raise BackupError("Staging must be a private directory outside the application tree")
    db = root / DB_REL
    if ((existing and not db.is_file()) or db.is_symlink() or (root / "data").is_symlink()):
        raise BackupError("An existing regular Ravuna SQLite file is required")
    return db


@contextmanager
def bounded_scratch(scratch, purpose):
    """Reap only the exact previous run directory under the same host lock."""
    if purpose not in {"full-run", "full-restore", "full-control", "db-run"}:
        raise BackupError("Unknown scratch scope")
    work = scratch / (".ravuna-recovery-" + purpose)
    if work.is_symlink() or work.is_mount() or not work.resolve().is_relative_to(scratch.resolve()):
        raise BackupError("Unsafe recovery scratch")
    if work.exists():
        marker = work / ".run-owned"
        state = work.stat()
        if (not work.is_dir() or (os.name == "posix" and
                (state.st_uid != os.getuid() or state.st_mode & 0o077))
                or any(p.is_symlink() or (p.is_dir() and p.is_mount()) for p in work.rglob("*"))
                or (any(work.iterdir()) and
                    (not marker.is_file() or marker.read_bytes() != b"ravuna-recovery-scratch-v1"))):
            raise BackupError("Unowned recovery scratch; refusing cleanup")
        shutil.rmtree(work)
    work.mkdir(mode=0o700)
    try:
        (work / ".run-owned").write_bytes(b"ravuna-recovery-scratch-v1")
        yield work
    finally:
        shutil.rmtree(work)




class S3:
    """Dedicated private bucket; no signed URLs or credentials in arguments."""

    def __init__(self, endpoint: str, bucket: str, region: str, *, checkpoints=False):
        url = urlsplit(endpoint)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.query or url.fragment or url.path not in {"", "/"}
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)):
            raise BackupError("Invalid independent S3 endpoint")
        self.bucket = bucket
        self.prefix = "ravuna/db-v2" if checkpoints else "ravuna/full-v3"
        self.checkpoints = checkpoints
        self.cli = ["aws", "--endpoint-url", endpoint, "--region", region, "--output", "json", "--no-cli-pager"]

    def _run(self, *args):
        result = subprocess.run([*self.cli, *args], capture_output=True, timeout=3600, check=False)
        if result.returncode:
            raise BackupError("Independent S3 operation failed")
        return result.stdout

    def _api(self, *args):
        raw = self._run("s3api", *args)
        return json.loads(raw) if raw.strip() else {}

    def ensure_bounded(self):
        # Versioning (even suspended) may keep deleted recovery objects.
        if self._api("get-bucket-versioning", "--bucket", self.bucket).get("Status"):
            raise BackupError("Dedicated recovery bucket versioning must never have been enabled")
        lifecycle = self._api("get-bucket-lifecycle-configuration", "--bucket", self.bucket)
        for rule in lifecycle.get("Rules", []):
            if rule.get("Status") != "Enabled":
                continue
            if any(key in rule for key in ("Expiration", "NoncurrentVersionExpiration", "Transitions")):
                raise BackupError("Automatic expiry/transition of completed objects is not allowed")
        allowed_prefixes = {"", "ravuna/", f"{self.prefix}/", f"{self.prefix}/objects/"}
        if not any(
            rule.get("Status") == "Enabled"
            and 1 <= rule.get("AbortIncompleteMultipartUpload", {}).get("DaysAfterInitiation", 0) <= 2
            and (not rule.get("Filter") or rule["Filter"] in [{"Prefix": p} for p in allowed_prefixes])
            and rule.get("Prefix", "") in allowed_prefixes
            for rule in lifecycle.get("Rules", [])
        ):
            raise BackupError("A bounded incomplete upload lifecycle is required")

    def get_catalog(self, work):
        key = f"{self.prefix}/catalog.json"
        listing = self._api("list-objects-v2", "--bucket", self.bucket, "--prefix", key)
        items = [item for item in listing.get("Contents", []) if item["Key"] == key]
        if not items:
            empty = {"version": 2 if self.checkpoints else 3, "points": [],
                     "pending": None, "retired": []}
            if not self.checkpoints:
                empty["scheduled_days"] = {}
            return empty, None
        if items[0]["Size"] > 8192:
            raise BackupError("Catalog exceeds bounds")
        path = work / "catalog.read.json"
        info = self._api("get-object", "--bucket", self.bucket, "--key", key, path)
        if path.stat().st_size > 8192:
            raise BackupError("Catalog exceeds bounds")
        return json.loads(path.read_bytes()), info["ETag"]

    def put_catalog(self, catalog, etag, work):
        path = work / "catalog.write.json"
        path.write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf-8")
        condition = ("--if-match", etag) if etag else ("--if-none-match", "*")
        self._api("put-object", "--bucket", self.bucket, "--key", f"{self.prefix}/catalog.json",
                  "--body", path, "--content-type", "application/json", *condition)

    def _key(self, slot):
        if not object_key(slot):
            raise BackupError("Invalid recovery object key")
        return f"{self.prefix}/objects/{slot}.enc"

    def put(self, slot, path):
        key = self._key(slot)
        common = ("--bucket", self.bucket, "--key", key)
        if path.stat().st_size <= 64 * 1024**2:
            try:
                self._api("put-object", *common, "--body", path, "--if-none-match", "*")
            except (BackupError, OSError, subprocess.SubprocessError, ValueError):
                raise UncertainMultipart("Object upload outcome is unknown") from None
            return
        try:
            upload = self._api("create-multipart-upload", *common)["UploadId"]
        except (BackupError, OSError, subprocess.SubprocessError, KeyError, ValueError):
            raise UncertainMultipart("Multipart initiation outcome is unknown") from None
        parts = []
        try:
            with tempfile.TemporaryDirectory(prefix="parts-", dir=path.parent) as work, path.open("rb") as src:
                part = Path(work) / "part"
                while chunk := src.read(64 * 1024**2):
                    part.write_bytes(chunk)
                    number = len(parts) + 1
                    result = self._api("upload-part", *common, "--upload-id", upload,
                                       "--part-number", str(number), "--body", part)
                    parts.append({"PartNumber": number, "ETag": result["ETag"]})
                manifest = Path(work) / "parts.json"
                manifest.write_text(json.dumps({"Parts": parts}), encoding="utf-8")
                self._api("complete-multipart-upload", *common, "--upload-id", upload,
                          "--multipart-upload", "file://" + str(manifest), "--if-none-match", "*")
        except Exception:
            # Lifecycle is the second line of defence if the writer is killed
            # or the abort call itself cannot reach storage.
            try:
                self._api("abort-multipart-upload", *common, "--upload-id", upload)
            except (BackupError, OSError, subprocess.SubprocessError, ValueError):
                raise UncertainMultipart("Multipart abort could not be confirmed") from None
            raise

    def get(self, slot, path, expected_size):
        common = ("--bucket", self.bucket, "--key", self._key(slot))
        info = self._api("head-object", *common)
        if info["ContentLength"] != expected_size:
            raise BackupError("External recovery size mismatch")
        self._api("get-object", *common, "--if-match", info["ETag"], path)

    def discard(self, slot):
        self._api("delete-object", "--bucket", self.bucket, "--key", self._key(slot))

    def abort_unfinished(self, slot):
        key = self._key(slot)
        listing = self._api("list-multipart-uploads", "--bucket", self.bucket,
                            "--prefix", key, "--max-uploads", "20", "--no-paginate")
        if listing.get("IsTruncated"):
            raise BackupError("Interrupted multipart listing exceeded its bound")
        for upload in listing.get("Uploads", []):
            if upload["Key"] != key:
                raise BackupError("Interrupted multipart key does not match")
            self._api("abort-multipart-upload", "--bucket", self.bucket, "--key", key,
                      "--upload-id", upload["UploadId"])
        remaining = self._api("list-multipart-uploads", "--bucket", self.bucket, "--prefix", key,
                              "--max-uploads", "20", "--no-paginate")
        if remaining.get("Uploads") or remaining.get("IsTruncated"):
            raise BackupError("Interrupted multipart upload still exists")


def _day(value):
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise BackupError("Noncanonical recovery day")
    return day


def _catalog(value):
    if not isinstance(value, dict):
        raise BackupError("Invalid recovery catalog")
    if set(value) != {"version", "points", "scheduled_days"} or value["version"] != 3:
        raise BackupError("Invalid recovery catalog")
    points = value["points"]
    if not isinstance(points, list) or len(points) > DAILY_POINTS + WEEKLY_POINTS:
        raise BackupError("Invalid recovery catalog count")
    seen = set()
    states = set()
    for point in points:
        fields = {"slot", "sha256", "size", "verified_at", "state_sha256"}
        if (not isinstance(point, dict) or set(point) != fields
                or not object_key(point["slot"]) or point["slot"] in seen
                or not re.fullmatch(r"[0-9a-f]{64}", point["sha256"])
                or type(point["size"]) is not int or not 0 < point["size"] <= CAP):
            raise BackupError("Invalid recovery point")
        if datetime.fromisoformat(point["verified_at"]).tzinfo is None:
            raise BackupError("Invalid recovery point timestamp")
        state = point.get("state_sha256")
        if not isinstance(state, str) or not re.fullmatch(r"[0-9a-f]{64}", state) or state in states:
            raise BackupError("Invalid or duplicate recovery state")
        states.add(state)
        seen.add(point["slot"])
    days = value["scheduled_days"]
    if not isinstance(days, dict) or len(days) > DAILY_POINTS + WEEKLY_POINTS:
        raise BackupError("Invalid recovery schedule history")
    for label, slot in days.items():
        _day(label)
        if slot not in seen:
            raise BackupError("Recovery date points to an absent slot")
    return points, days


def _validate_points(value):
    return _catalog(value)[0]


def _select_days(days, current_day):
    labels = sorted(days, reverse=True)
    selected = set(labels[:DAILY_POINTS])
    week_start = current_day - timedelta(days=current_day.weekday())
    for weeks_ago in range(1, WEEKLY_POINTS + 1):
        start = week_start - timedelta(weeks=weeks_ago)
        choice = next((label for label in labels if start <= _day(label) < start + timedelta(days=7)), None)
        if choice:
            selected.add(choice)
    return {label: days[label] for label in sorted(selected, reverse=True)}


def _read_point(store, point, destination, inspect=artifact):
    store.get(point["slot"], destination, point["size"])
    received = inspect(destination)
    if received["sha256"] != point["sha256"] or received["size_bytes"] != point["size"]:
        raise BackupError("Recovery readback failed integrity verification")


def promote(store, work, archive, verify, scheduled=False, now=_utcnow):
    with reserve(store, work, _validate_points) as txn:
        return _promote_reserved(store, work, archive, verify, txn, scheduled, now)


def _validate_schedule(points, days, scheduled):
    start_day = scheduled if isinstance(scheduled, str) else None
    if start_day:
        _day(start_day)
        if days and start_day <= max(days):
            raise BackupError("A scheduled recovery is already recorded for this or a later day")
    elif len(points) >= MIN_POINTS:
        raise BackupError("Manual recovery cannot rotate an established schedule")
    return start_day


def _promote_reserved(store, work, archive, verify, txn, scheduled, now):
    points, days = _catalog(txn.current)
    start_day = _validate_schedule(points, days, scheduled)
    candidate = txn.key
    initial = artifact(archive)
    if not 0 < initial["size_bytes"] <= CAP:
        raise BackupError("Recovery candidate exceeds the slot budget")
    # The new verified point must leave a still-readable previous point.
    if points:
        previous_root = work / "previous"
        previous_root.mkdir(exist_ok=True)
        previous = previous_root / READBACK
        _read_point(store, points[0], previous)
        previous.unlink()
    store.put(candidate, archive)
    archive.unlink()
    store.get(candidate, archive, initial["size_bytes"])
    received = artifact(archive)
    if any(received[key] != initial[key] for key in ("sha256", "size_bytes")):
        raise BackupError("New recovery failed full readback")
    proof = verify(initial["sha256"])
    state = proof.get("state_sha256") if isinstance(proof, dict) else None
    if not isinstance(state, str) or not re.fullmatch(r"[0-9a-f]{64}", state):
        raise BackupError("Recovery has no verified content identity")
    timestamp = now()
    new = {"slot": candidate, "size": initial["size_bytes"],
           "sha256": initial["sha256"], "verified_at": timestamp.isoformat(), "state_sha256": state}
    duplicate = next((p for p in points if p["state_sha256"] == state), None)
    if duplicate and duplicate == points[0] and len(points) >= MIN_POINTS:
        # The newest kept payload was already verified; check a *distinct*
        # fallback too before repointing any schedule labels to the duplicate.
        _read_point(store, points[1], previous)
        previous.unlink()
    if duplicate:
        days = {label: candidate if slot == duplicate["slot"] else slot for label, slot in days.items()}
    if start_day:
        days = _select_days({**days, start_day: candidate}, _day(start_day))
    kept = [new] + [p for p in points if p != duplicate and p["slot"] in set(days.values())]
    # Keep a distinct fallback while bringing an empty/legacy catalog up to
    # date. An identical payload never counts as a second recovery point.
    for old in points:
        if len(kept) >= MIN_POINTS:
            break
        if old != duplicate and old not in kept:
            kept.append(old)
    updated = {"version": 3, "points": kept, "scheduled_days": days}
    txn.publish(updated)
    return {"verified_points": len(kept), "scheduled_day": start_day}


def watchdog(store, work, verify, now=None):
    now = now or _utcnow()
    catalog, _etag = store.get_catalog(work)
    points, days = _catalog(control(catalog, _validate_points))
    samara = now.astimezone(SAMARA)
    expected = samara.date() if samara.hour >= 6 else samara.date() - timedelta(days=1)
    point = next((p for p in points if p["slot"] == days.get(expected.isoformat())), None)
    if (len(points) < MIN_POINTS or not point
            or not timedelta() <= now - datetime.fromisoformat(point["verified_at"]) <= timedelta(hours=27)):
        raise BackupError("Scheduled RU recovery is missing, stale or incomplete")
    scratch = work / READBACK
    _read_point(store, point, scratch)
    proof = verify(point["sha256"])
    if not isinstance(proof, dict) or proof.get("state_sha256") != point["state_sha256"]:
        raise BackupError("Independent restore did not match the scheduled state")
    return {"verified_points": len(points), "independent_restore": "PASS", "scheduled_day": expected.isoformat()}


def _scheduled_day(start):
    local = start.astimezone(SAMARA)
    if local.hour != 3 or local.minute >= 15:
        return None
    try:
        data = subprocess.run([
            "busctl", "--system", "get-property", "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1/unit/ravuna_2dru_2drecovery_2etimer",
            "org.freedesktop.systemd1.Timer", "LastTriggerUSec",
        ], capture_output=True, text=True, timeout=3, check=True).stdout
        kind, value = data.split()
        triggered = datetime.fromtimestamp(int(value) / 1_000_000, timezone.utc)
        if kind == "t" and timedelta() <= start - triggered <= timedelta(minutes=2):
            return local.date().isoformat()
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        pass
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "daily", "watchdog", "recover-writer"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--passphrase-file", type=Path)
    parser.add_argument("--writers-fenced", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--interrupted-key")
    parser.add_argument("--endpoint")
    parser.add_argument("--bucket")
    parser.add_argument("--region")
    args = parser.parse_args(argv)
    try:
        root, scratch = args.root.absolute(), args.scratch.absolute()
        db = _state(root, scratch, existing=args.command not in {"restore", "watchdog", "recover-writer"})
        if not args.passphrase_file and args.command != "recover-writer":
            raise BackupError("Passphrase file is required")
        phrase = args.passphrase_file.read_text(encoding="utf-8").rstrip("\r\n") if args.passphrase_file else ""
        if args.command == "restore":
            if (not args.archive or not args.destination or not args.sha256
                    or not re.fullmatch(r"[0-9a-f]{64}", args.sha256)
                    or args.destination.is_symlink() or args.destination.exists()
                    or args.archive.is_symlink() or not args.archive.is_file()
                    or args.archive.stat().st_size > CAP):
                raise BackupError("Restore requires an encrypted archive, hash and a new isolated directory")
            if args.destination.parent.resolve() != scratch.resolve():
                raise BackupError("Restore must be a new child of the private scratch directory")
            with SingleInstanceLock(scratch / "ru-recovery.lock"), bounded_scratch(scratch, "full-restore") as work:
                if shutil.disk_usage(scratch).free < MIN_FREE:
                    raise BackupError("RU recovery disk headroom is below 16 GiB")
                manager = BackupManager(db, Path(work), 14)
                shutil.copyfile(args.archive, manager.backup_dir / args.archive.name)
                # Validate fully before atomically exposing the restored tree.
                staged = Path(work) / "verify"
                result = _restore(manager, args.archive.name, phrase, staged, args.sha256)
                if args.destination.exists() or args.destination.is_symlink():
                    raise BackupError("Restore destination appeared during verification")
                staged.rename(args.destination)
        elif args.command == "recover-writer":
            if not args.writers_fenced:
                raise BackupError("All writers must be fenced before clearing an interrupted backup")
            store = S3(args.endpoint or "", args.bucket or "", args.region or "")
            with SingleInstanceLock(scratch / "ru-recovery.lock"), bounded_scratch(scratch, "full-control") as work:
                store.ensure_bounded()
                recover_interrupted_writer(store, Path(work), _validate_points, args.interrupted_key)
            result = {"interrupted_writer_cleared": "PASS"}
        else:
            store = S3(args.endpoint or "", args.bucket or "", args.region or "")
            with SingleInstanceLock(scratch / "ru-recovery.lock"):
                store.ensure_bounded()
                with bounded_scratch(scratch, "full-run") as work:
                    if shutil.disk_usage(scratch).free < MIN_FREE:
                        raise BackupError("RU recovery disk headroom is below 16 GiB")
                    if args.command == "watchdog":
                        manager = BackupManager(db, work, 14)
                        result = watchdog(store, work, lambda h: _restore(
                            manager, READBACK, phrase, work / "restore", h))
                    else:
                        scheduled = _scheduled_day(_utcnow()) if args.scheduled else None
                        if args.scheduled and not scheduled:
                            raise BackupError("A scheduled recovery needs a verified timer trigger")
                        manager = BackupManager(db, work / "archive", 14)
                        with reserve(store, work, _validate_points) as txn:
                            points, days = _catalog(txn.current)
                            _validate_schedule(points, days, scheduled)
                            # Prevent retention from unlinking files between
                            # the SQLite snapshot and their archive read.
                            with media_guard(db.parent), _ciphertext_cap(CAP):
                                metadata = manager.create_recovery_bundle(
                                    phrase, daily_ciphertext_limit=CAP, _expanded_byte_cap=EXPANDED_CAP,
                                    _standalone_sqlite=True)
                            filename = metadata["recovery_bundle_name"]
                            result = _promote_reserved(
                                store, work, manager.backup_dir / filename,
                                lambda h: _restore(manager, filename, phrase, work / "restore", h),
                                txn, scheduled, _utcnow)
        print(json.dumps({"status": "PASS", **result}, sort_keys=True))
        return 0
    except (BackupError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
