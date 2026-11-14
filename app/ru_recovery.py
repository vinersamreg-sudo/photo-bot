"""RU recovery and offline migration, separate from the deployed backup workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.backup import BackupError, BackupManager
from app.database import CURRENT_SCHEMA_VERSION, ReadOnlyDatabase
from app.max_transport import SingleInstanceLock
from app.payment_admin import payment_reconciliation_summary
from app.recovery_retention import artifact

SAMARA = timezone(timedelta(hours=4))
SLOTS = ("a", "b", "c")
MIN_FREE = 16 * 1024**3
CAP = 8 * 1024**3
DB_REL = "data/photo_bot.sqlite3"
READBACK = "ravuna-recovery-20000101T000000000000Z.tar.gz.enc"


def _utcnow():
    return datetime.now(timezone.utc)


def _proof(root: Path, expected_hash: str):
    database = root / "database/photo_bot.sqlite3"
    reports = payment_reconciliation_summary(ReadOnlyDatabase(database))
    if reports["status"] == "CRITICAL":
        raise BackupError("Restored financial ledger does not reconcile")
    with ReadOnlyDatabase(database).read() as conn:
        counts = {name: int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
                  for name in ("users", "gallery_items", "gallery_versions", "payment_orders",
                               "continuation_pack_grants", "credit_ledger", "referral_bonus_grants",
                               "legal_consents", "max_legal_acceptances")}
        if conn.execute("""SELECT 1 FROM user_credit_accounts
                           WHERE available_generation_credits<0 OR reserved_generation_credits<0""").fetchone():
            raise BackupError("Restored credit balance is negative")
    return {"sha256": expected_hash, "counts": counts, "payments": reports["status"]}


def _restore(manager, filename, passphrase, dest, expected_hash):
    if artifact(manager.backup_dir / filename)["sha256"] != expected_hash:
        raise BackupError("Ciphertext hash mismatch")
    report = manager.restore_recovery_bundle(Path(filename), passphrase, restore_root=dest)
    if report.get("overall") != "PASS" or report.get("migration") != CURRENT_SCHEMA_VERSION:
        raise BackupError("Restore or expected schema verification failed")
    encoded = json.dumps(report["commerce_fingerprints"], sort_keys=True).encode()
    return {**_proof(dest, expected_hash), "commerce_sha256": hashlib.sha256(encoded).hexdigest()}


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


def _assert_drained(db):
    with ReadOnlyDatabase(db).read() as conn:
        checks = (
            "SELECT 1 FROM generation_attempts WHERE status IN ('pending','processing')",
            "SELECT 1 FROM generation_credit_reservations WHERE status='reserved'",
            "SELECT 1 FROM max_dialogs WHERE state='processing'",
            "SELECT 1 FROM max_processed_events WHERE status='processing'",
        )
        if any(conn.execute(query).fetchone() for query in checks):
            raise BackupError("Writer state must be fenced and drained")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='max_webhook_inbox'").fetchone():
            if conn.execute("SELECT 1 FROM max_webhook_inbox WHERE payload IS NOT NULL").fetchone():
                raise BackupError("Drain the MAX inbox before the final snapshot")


def export_migration(root, scratch, passphrase):
    db = _state(root, scratch)
    with SingleInstanceLock(root / "data/max-polling.lock"):
        _assert_drained(db)
        with tempfile.TemporaryDirectory(dir=scratch) as work:
            manager = BackupManager(db, Path(work) / "archive", 14)
            metadata = manager.create_recovery_bundle(passphrase)
            name = metadata["recovery_bundle_name"]
            report = _restore(manager, name, passphrase, Path(work) / "proof", metadata["sha256"])
            destination = scratch / name
            if destination.exists():
                raise BackupError("Migration export already exists")
            (manager.backup_dir / name).rename(destination)
            return {**report, "archive": name}


class S3:
    """Dedicated private bucket; no signed URLs or credentials in arguments."""

    def __init__(self, endpoint: str, bucket: str, region: str):
        url = urlsplit(endpoint)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.query or url.fragment or url.path not in {"", "/"}
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)):
            raise BackupError("Invalid independent S3 endpoint")
        self.bucket = bucket
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
        # Versioning (even suspended) may keep more than the three fixed slots.
        if self._api("get-bucket-versioning", "--bucket", self.bucket).get("Status"):
            raise BackupError("Dedicated recovery bucket versioning must never have been enabled")
        lifecycle = self._api("get-bucket-lifecycle-configuration", "--bucket", self.bucket)
        for rule in lifecycle.get("Rules", []):
            if rule.get("Status") != "Enabled":
                continue
            if any(key in rule for key in ("Expiration", "NoncurrentVersionExpiration", "Transitions")):
                raise BackupError("Automatic expiry/transition of completed objects is not allowed")
        if not any(
            rule.get("Status") == "Enabled"
            and 1 <= rule.get("AbortIncompleteMultipartUpload", {}).get("DaysAfterInitiation", 0) <= 2
            and (rule.get("Filter", {}) in ({}, {"Prefix": ""}, {"Prefix": "ravuna/"}, {"Prefix": "ravuna/slots/"})
                 and rule.get("Prefix", "") in {"", "ravuna/", "ravuna/slots/"})
            for rule in lifecycle.get("Rules", [])
        ):
            raise BackupError("A bounded incomplete upload lifecycle is required")

    def get_catalog(self, work):
        listing = self._api("list-objects-v2", "--bucket", self.bucket, "--prefix", "ravuna/catalog.json")
        items = [item for item in listing.get("Contents", []) if item["Key"] == "ravuna/catalog.json"]
        if not items:
            return {"points": [], "scheduled_day": None}, None
        if items[0]["Size"] > 8192:
            raise BackupError("Catalog exceeds bounds")
        path = work / "catalog.read.json"
        info = self._api("get-object", "--bucket", self.bucket, "--key", "ravuna/catalog.json", path)
        return json.loads(path.read_bytes()), info["ETag"]

    def put_catalog(self, catalog, etag, work):
        path = work / "catalog.write.json"
        path.write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf-8")
        condition = ("--if-match", etag) if etag else ("--if-none-match", "*")
        self._api("put-object", "--bucket", self.bucket, "--key", "ravuna/catalog.json",
                  "--body", path, "--content-type", "application/json", *condition)

    def _uri(self, slot):
        if slot not in SLOTS:
            raise BackupError("Unknown recovery slot")
        return f"s3://{self.bucket}/ravuna/slots/{slot}.enc"

    def put(self, slot, path):
        self._run("s3", "cp", path, self._uri(slot), "--only-show-errors")

    def get(self, slot, path, expected_size):
        self._uri(slot)
        info = self._api("head-object", "--bucket", self.bucket, "--key", f"ravuna/slots/{slot}.enc")
        if info["ContentLength"] != expected_size:
            raise BackupError("External recovery size mismatch")
        self._run("s3", "cp", self._uri(slot), path, "--only-show-errors")

    def discard(self, slot):
        self._run("s3", "rm", self._uri(slot), "--only-show-errors")


def _catalog(value):
    if not isinstance(value, dict) or set(value) != {"points", "scheduled_day"}:
        raise BackupError("Invalid recovery catalog")
    points = value["points"]
    if not isinstance(points, list) or len(points) > 2:
        raise BackupError("Invalid recovery catalog count")
    if value["scheduled_day"] is not None:
        datetime.strptime(value["scheduled_day"], "%Y-%m-%d")
    seen = set()
    for point in points:
        if (not isinstance(point, dict) or set(point) != {"slot", "sha256", "size", "verified_at"}
                or point["slot"] not in SLOTS or point["slot"] in seen
                or not re.fullmatch(r"[0-9a-f]{64}", point["sha256"])
                or type(point["size"]) is not int or not 0 < point["size"] <= CAP):
            raise BackupError("Invalid recovery point")
        if datetime.fromisoformat(point["verified_at"]).tzinfo is None:
            raise BackupError("Invalid recovery point timestamp")
        seen.add(point["slot"])
    return points


def promote(store, work, archive, verify, scheduled=False, now=_utcnow):
    current, etag = store.get_catalog(work)
    points = _catalog(current)
    candidate = next(slot for slot in SLOTS if slot not in {p["slot"] for p in points})
    initial = artifact(archive)
    if not 0 < initial["size_bytes"] <= CAP:
        raise BackupError("Recovery candidate exceeds the slot budget")
    # The new verified point must leave a still-readable previous point.
    if points:
        previous_root = work / "previous"
        previous_root.mkdir(exist_ok=True)
        previous = previous_root / READBACK
        store.get(points[0]["slot"], previous, points[0]["size"])
        identity = artifact(previous)
        if identity["sha256"] != points[0]["sha256"] or identity["size_bytes"] != points[0]["size"]:
            raise BackupError("Previous recovery failed full readback")
        previous.unlink()
    store.put(candidate, archive)
    archive.unlink()
    store.get(candidate, archive, initial["size_bytes"])
    received = artifact(archive)
    if any(received[key] != initial[key] for key in ("sha256", "size_bytes")):
        raise BackupError("New recovery failed full readback")
    verify(initial["sha256"])
    date = now()
    start_day = scheduled if isinstance(scheduled, str) else None
    new = {"slot": candidate, "size": initial["size_bytes"],
           "sha256": initial["sha256"], "verified_at": date.isoformat()}
    store.put_catalog({"points": [new, *points[:1]],
                       "scheduled_day": start_day or current["scheduled_day"]}, etag, work)
    # No deletion before catalog publication. On failure there are at most
    # three slots; next execution reuses the unreferenced slot.
    if len(points) == 2:
        store.discard(points[1]["slot"])
    return {"verified_points": min(len(points) + 1, 2), "scheduled_day": start_day}


def watchdog(store, work, now=None):
    now = now or _utcnow()
    catalog, _etag = store.get_catalog(work)
    points = _catalog(catalog)
    samara = now.astimezone(SAMARA)
    expected = samara.date() if samara.hour >= 6 else samara.date() - timedelta(days=1)
    if (len(points) != 2 or catalog["scheduled_day"] != expected.isoformat()
            or not timedelta() <= now - datetime.fromisoformat(points[0]["verified_at"]) <= timedelta(hours=27)):
        raise BackupError("Scheduled RU recovery is missing, stale or incomplete")
    for point in points:
        scratch = work / READBACK
        store.get(point["slot"], scratch, point["size"])
        if artifact(scratch)["sha256"] != point["sha256"]:
            raise BackupError("RU watchdog found an unreadable point")
        scratch.unlink()


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
    parser.add_argument("command", choices=("export", "restore", "daily", "watchdog"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--passphrase-file", type=Path)
    parser.add_argument("--writers-fenced", action="store_true")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--endpoint")
    parser.add_argument("--bucket")
    parser.add_argument("--region")
    args = parser.parse_args(argv)
    try:
        root, scratch = args.root.absolute(), args.scratch.absolute()
        db = _state(root, scratch, existing=args.command not in {"restore", "watchdog"})
        if not args.passphrase_file and args.command != "watchdog":
            raise BackupError("Passphrase file is required")
        phrase = args.passphrase_file.read_text(encoding="utf-8").rstrip("\r\n") if args.passphrase_file else ""
        if args.command == "export":
            if not args.writers_fenced:
                raise BackupError("Offline migration export requires the explicit writer fence")
            result = export_migration(root, scratch, phrase)
        elif args.command == "restore":
            if (not args.archive or not args.destination or not args.sha256
                    or not re.fullmatch(r"[0-9a-f]{64}", args.sha256)
                    or args.destination.is_symlink() or args.destination.exists()
                    or args.archive.is_symlink()):
                raise BackupError("Restore requires an encrypted archive, hash and a new isolated directory")
            if args.destination.parent.resolve() != scratch.resolve():
                raise BackupError("Restore must be a new child of the private scratch directory")
            with tempfile.TemporaryDirectory(dir=scratch) as work:
                manager = BackupManager(db, Path(work), 14)
                shutil.copyfile(args.archive, manager.backup_dir / args.archive.name)
                # Validate fully before atomically exposing the restored tree.
                staged = Path(work) / "verify"
                result = _restore(manager, args.archive.name, phrase, staged, args.sha256)
                if args.destination.exists() or args.destination.is_symlink():
                    raise BackupError("Restore destination appeared during verification")
                staged.rename(args.destination)
        else:
            store = S3(args.endpoint or "", args.bucket or "", args.region or "")
            with SingleInstanceLock(scratch / "ru-recovery.lock"):
                store.ensure_bounded()
                if shutil.disk_usage(scratch).free < MIN_FREE:
                    raise BackupError("RU recovery disk headroom is below 16 GiB")
                with tempfile.TemporaryDirectory(dir=scratch) as work:
                    work = Path(work)
                    if args.command == "watchdog":
                        watchdog(store, work)
                        result = {"verified_points": 2}
                    else:
                        scheduled = _scheduled_day(_utcnow()) if args.scheduled else None
                        manager = BackupManager(db, work / "archive", 14)
                        metadata = manager.create_recovery_bundle(phrase, daily_ciphertext_limit=CAP)
                        filename = metadata["recovery_bundle_name"]
                        result = promote(store, work, manager.backup_dir / filename,
                                         lambda h: _restore(manager, filename, phrase, work / "restore", h),
                                         scheduled=scheduled)
        print(json.dumps({"status": "PASS", **result}, sort_keys=True))
        return 0
    except (BackupError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
