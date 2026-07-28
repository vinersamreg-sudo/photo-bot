"""Encrypted, restore-tested SQLite backups for Ravuna operations.

The ``pixora-`` filename prefix is retained as an immutable backup-format
identifier so existing encrypted backup rotation and restore discovery remain
compatible across the brand migration.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class BackupError(RuntimeError):
    """A safe operational backup failure without secret material."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


class BackupManager:
    """Create encrypted copies and prove that they can be restored."""

    def __init__(self, database_path: Path, backup_dir: Path, retention_days: int) -> None:
        self.database_path = database_path
        self.backup_dir = backup_dir
        self.retention_days = retention_days
        backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            backup_dir.chmod(0o700)
        except OSError:
            pass

    @staticmethod
    def _require_passphrase(passphrase: str) -> None:
        if len(passphrase) < 20:
            raise BackupError("Backup passphrase must contain at least 20 characters")

    @staticmethod
    def _openssl(*args: str, passphrase: str) -> None:
        try:
            result = subprocess.run(
                ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", *args,
                 "-pass", "stdin"],
                input=passphrase + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise BackupError("OpenSSL is unavailable") from exc
        if result.returncode != 0:
            raise BackupError("Backup encryption operation failed")

    @staticmethod
    def _validate_sqlite(path: Path) -> dict[str, Any]:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                migration = connection.execute(
                    "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise BackupError("Restored SQLite database could not be opened") from exc
        if not quick_check or quick_check[0] != "ok":
            raise BackupError("Restored SQLite integrity check failed")
        return {"quick_check": "ok", "migration": int(migration[0])}

    def create(self, passphrase: str) -> dict[str, Any]:
        self._require_passphrase(passphrase)
        if not self.database_path.is_file():
            raise BackupError("SQLite database does not exist")
        created_at = _now()
        name = f"pixora-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3.enc"
        encrypted = self.backup_dir / name
        with tempfile.TemporaryDirectory(dir=self.backup_dir) as temporary_dir:
            plain = Path(temporary_dir) / "snapshot.sqlite3"
            source = sqlite3.connect(self.database_path)
            target = sqlite3.connect(plain)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            self._validate_sqlite(plain)
            self._openssl("-salt", "-in", str(plain), "-out", str(encrypted), passphrase=passphrase)
        digest_builder = hashlib.sha256()
        with encrypted.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest_builder.update(chunk)
        digest = digest_builder.hexdigest()
        try:
            encrypted.chmod(0o600)
        except OSError:
            pass
        metadata = {
            "backup_name": name,
            "created_at": created_at.isoformat(),
            "encrypted": True,
            "size_bytes": encrypted.stat().st_size,
            "sha256": digest,
        }
        _write_json(self.backup_dir / "latest.json", metadata)
        self.prune(created_at)
        return metadata

    def restore_test(self, backup: Path, passphrase: str) -> dict[str, Any]:
        self._require_passphrase(passphrase)
        if backup.is_absolute() or backup.name != str(backup):
            raise BackupError("Backup must be referenced by file name")
        candidate = self.backup_dir / backup.name
        if not candidate.is_file() or candidate.suffix != ".enc":
            raise BackupError("Encrypted backup was not found")
        with tempfile.TemporaryDirectory(dir=self.backup_dir) as temporary_dir:
            restored = Path(temporary_dir) / "restored.sqlite3"
            self._openssl("-d", "-in", str(candidate), "-out", str(restored), passphrase=passphrase)
            result = self._validate_sqlite(restored)
        report = {
            "backup_name": candidate.name,
            "tested_at": _now().isoformat(),
            "restore_ok": True,
            **result,
        }
        _write_json(self.backup_dir / "restore_status.json", report)
        return report

    def mark_offsite(self, backup_name: str, provider: str) -> dict[str, Any]:
        candidate = self.backup_dir / Path(backup_name).name
        if not candidate.is_file():
            raise BackupError("Backup to mark off-site was not found")
        report = {
            "backup_name": candidate.name,
            "provider": provider,
            "copied_at": _now().isoformat(),
        }
        _write_json(self.backup_dir / "offsite_status.json", report)
        return report

    def prune(self, now: datetime | None = None) -> int:
        cutoff = (now or _now()) - timedelta(days=self.retention_days)
        removed = 0
        for candidate in self.backup_dir.glob("pixora-*.sqlite3.enc"):
            if candidate.is_symlink():
                continue
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                candidate.unlink()
                removed += 1
        return removed
