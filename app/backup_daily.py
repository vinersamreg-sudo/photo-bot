"""Bounded, run-owned staging for the scheduled encrypted recovery cycle."""
from __future__ import annotations

import os
import re
import shutil
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.backup import BackupError, BackupManager, _write_json
from app.recovery_retention import NAME, _lock, artifact, read_json, validate

STAGE = ".daily-recovery-pending-v1"
LEASE_SECONDS = 2 * 3600  # Longer than the workflow's complete 60-minute deadline.
SQLITE_NAME = re.compile(r"pixora-\d{8}T\d{12}Z\.sqlite3\.enc\Z")
SAMARA = timezone(timedelta(hours=4))


def _samara_today():
    return datetime.now(SAMARA).date()


def _owner(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+-[0-9]+", value):
        raise BackupError("Invalid daily recovery run identifier")
    return value


def _allocated(directory):
    total = 0
    for path in directory.rglob("*"):
        value = path.lstat()
        if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
            raise BackupError("Backup budget cannot account for an unsafe entry")
        total += max(value.st_size, getattr(value, "st_blocks", 0) * 512)
    return total


def _sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def _ciphertext_cap(limit):
    if os.name != "posix":
        yield  # Production is Linux; Windows development uses post-write assertion.
        return
    import resource
    previous = resource.getrlimit(resource.RLIMIT_FSIZE)
    soft = min(limit, previous[0]) if previous[0] != resource.RLIM_INFINITY else limit
    resource.setrlimit(resource.RLIMIT_FSIZE, (soft, previous[1]))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, previous)


class DailyRecovery:
    def __init__(self, manager, owner):
        self.manager = manager
        self.root = manager.backup_dir
        self.stage = self.root / STAGE
        self.owner = _owner(owner)

    def _existing_owner(self):
        if self.stage.is_symlink():
            raise BackupError("Daily recovery staging is unsafe")
        if not self.stage.exists():
            return None
        st = self.stage.stat()
        if not stat.S_ISDIR(st.st_mode) or (os.name == "posix" and
                (st.st_uid != os.getuid() or st.st_mode & 0o077)):
            raise BackupError("Daily recovery staging is unsafe")
        lease = self.stage / "lease.json"
        if not lease.exists():
            # A crash between mkdir and the lease publication cannot create an
            # archive. Recover only this exact empty/new staging location.
            entries = list(self.stage.iterdir())
            if all(p.name.startswith(".lease.json.") and p.name.endswith(".tmp")
                   and not p.is_symlink() and p.is_file() for p in entries):
                return ("incomplete", st.st_mtime)
            raise BackupError("Daily recovery staging has no safe ownership proof")
        state = read_json(lease)
        return _owner(state["owner"]), float(state["started"])

    def _discard(self):
        # No other production directory may ever enter this cleanup path.
        if self.stage != self.root / STAGE or self.stage.is_symlink() or self.stage.is_mount():
            raise BackupError("Daily recovery staging is unsafe")
        if not self.stage.resolve().is_relative_to(self.root.resolve()):
            raise BackupError("Daily recovery staging is unsafe")
        shutil.rmtree(self.stage)
        _sync_directory(self.root)

    def cleanup(self):
        with _lock(self.root):
            state = self._existing_owner()
            if state and state[0] == self.owner:
                self._discard()
                return {"cleaned": True}
            return {"cleaned": False}

    def _staged(self):
        return BackupManager(self.manager.database_path, self.stage, self.manager.retention_days)

    def create(self, password):
        self.manager._require_passphrase(password)
        with _lock(self.root):
            state = self._existing_owner()
            if state:
                if state[0] == self.owner and (self.stage / "ready.json").is_file():
                    return read_json(self.stage / "ready.json")
                if state[0] not in {self.owner, "incomplete"} and time.time() - state[1] < LEASE_SECONDS:
                    raise BackupError("Another daily recovery run holds the staging lease")
                self._discard()
            # Don't trust a latest pointer across a kill during publication.
            today = _samara_today()
            for file in self.root.glob("ravuna-recovery-*.tar.gz.enc"):
                match = NAME.fullmatch(file.name)
                if not match:
                    continue
                created = datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
                if created.astimezone(SAMARA).date() != today:
                    continue
                receipt_path = file.with_name(file.name + ".proof.json")
                if receipt_path.is_file():
                    receipt = read_json(receipt_path)
                    if receipt.get("offsite", {}).get("overall") == "PASS":
                        validate(receipt, file.name, artifact(file))
                        raise BackupError("A verified recovery already exists for this Samara day")
            capacity = self.manager.daily_capacity(_allocated(self.root))
            self.stage.mkdir(mode=0o700)
            try:
                _write_json(self.stage / "lease.json", {"owner": self.owner, "started": time.time()})
                _sync_directory(self.stage)
                staged = self._staged()
                with _ciphertext_cap(capacity["ciphertext_limit"]):
                    sqlite = staged.create(password)
                    sqlite_proof = staged.restore_test(Path(sqlite["backup_name"]), password)
                    recovery = staged.create_recovery_bundle(
                        password, daily_ciphertext_limit=capacity["ciphertext_limit"],
                    )
                    restore = staged.restore_recovery_bundle(
                        Path(recovery["recovery_bundle_name"]), password,
                        _bounded_member_limit=capacity["member_limit"], _scratch_parent=self.stage,
                    )
                if (restore["overall"] != "PASS" or sqlite_proof["quick_check"] != "ok"
                        or restore["foreign_key_check"] != "ok"
                        or sqlite_proof["migration"] != restore["migration"]
                        or restore["source_revision"] != self.manager._source_revision()
                        or not restore["commerce_fingerprints"].get("payment_orders")
                        or min(restore["gallery_files"].values()) <= 0):
                    raise BackupError("Daily recovery restore did not pass")
                ready = {"backup_name": sqlite["backup_name"], "backup_sha": sqlite["sha256"],
                         "recovery_name": recovery["recovery_bundle_name"],
                         "recovery_sha": recovery["sha256"], "migration": restore["migration"],
                         "restore_method": restore["restore_method"], "capacity": capacity}
                _write_json(self.stage / "ready.json", ready)
                return ready
            except BaseException:
                self._discard()
                raise

    def accept(self, provider, expected_backup_sha, expected_recovery_sha):
        if not isinstance(provider, str) or not re.fullmatch(r"github-actions:[0-9]+:[0-9]+", provider):
            raise BackupError("Daily recovery requires a specific off-site artifact")
        with _lock(self.root):
            state = self._existing_owner()
            if not state or state[0] != self.owner:
                raise BackupError("Daily recovery staging lease is missing")
            ready = read_json(self.stage / "ready.json")
            sqlite, recovery = ready["backup_name"], ready["recovery_name"]
            if (not SQLITE_NAME.fullmatch(sqlite) or not NAME.fullmatch(recovery)
                    or ready["backup_sha"] != expected_backup_sha
                    or ready["recovery_sha"] != expected_recovery_sha):
                raise BackupError("Daily recovery readback does not match staged metadata")
            staged = self._staged()
            sqlite_path = self.stage / sqlite
            if (sqlite_path.is_symlink() or not sqlite_path.is_file()
                    or staged._sha256(sqlite_path) != expected_backup_sha
                    or artifact(self.stage / recovery)["sha256"] != expected_recovery_sha):
                raise BackupError("Daily recovery ciphertext changed before acceptance")
            staged.mark_offsite(sqlite, provider)
            receipt = recovery + ".proof.json"
            proof = read_json(self.stage / receipt)
            if not proof.get("offsite"):
                staged.mark_recovery_offsite(recovery, provider)
                proof = read_json(self.stage / receipt)
            if proof["offsite"].get("provider") != provider:
                raise BackupError("Daily recovery off-site identity changed")
            validate(proof, recovery, artifact(self.stage / recovery))
            # Publish proof before ciphertext; the discovery/retention planner
            # only considers a recovery when its ciphertext name is visible.
            for name in (receipt, recovery, sqlite):
                source, target = self.stage / name, self.root / name
                with source.open("rb+") as stream:
                    os.fsync(stream.fileno())
                try:
                    os.link(source, target, follow_symlinks=False)
                except FileExistsError:
                    if target.is_symlink() or not os.path.samefile(source, target):
                        raise BackupError("Daily recovery publication collision") from None
                _sync_directory(self.root)
            for name in ("latest.json", "latest_recovery.json", "restore_status.json",
                         "offsite_status.json", "recovery_restore_status.json",
                         "recovery_offsite_status.json"):
                _write_json(self.root / name, read_json(self.stage / name))
            _sync_directory(self.root)
            self._discard()
            self.manager.prune_sqlite_by_age()
        return {"recovery_name": recovery, "accepted": True}
