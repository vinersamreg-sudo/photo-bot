"""Encrypted, restore-tested SQLite backups for Ravuna operations.

The ``pixora-`` filename prefix is retained as an immutable backup-format
identifier so existing encrypted backup rotation and restore discovery remain
compatible across the brand migration.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


RECOVERY_BUNDLE_VERSION = 1
RECOVERY_COMPONENTS = (
    "database/photo_bot.sqlite3",
    "private_storage/users",
)


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
        self.base_dir = database_path.parent.parent.resolve()
        self.users_dir = database_path.parent / "users"
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

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _sqlite_snapshot(self, destination: Path) -> dict[str, Any]:
        source = sqlite3.connect(self.database_path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return self._validate_sqlite(destination)

    def _source_revision(self) -> str | None:
        for path in (
            self.base_dir / ".deploy-sha",
            self.base_dir / "data" / "deployed_commit.txt",
        ):
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
                return value.lower()
        return None

    @staticmethod
    def _validate_recovery_manifest(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise BackupError("Recovery manifest is invalid")
        if value.get("bundle_version") != RECOVERY_BUNDLE_VERSION:
            raise BackupError("Recovery manifest version is unsupported")
        if value.get("encrypted") is not True:
            raise BackupError("Recovery manifest encryption status is invalid")
        created_at = value.get("created_at")
        if not isinstance(created_at, str):
            raise BackupError("Recovery manifest timestamp is invalid")
        try:
            parsed_created_at = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise BackupError("Recovery manifest timestamp is invalid") from exc
        if parsed_created_at.tzinfo is None:
            raise BackupError("Recovery manifest timestamp is invalid")
        source_revision = value.get("source_revision")
        if not isinstance(source_revision, str) or not re.fullmatch(
            r"[0-9a-f]{7,40}", source_revision
        ):
            raise BackupError("Recovery manifest source revision is invalid")
        included = value.get("included_components")
        if (
            not isinstance(included, list)
            or not all(isinstance(component, str) for component in included)
            or set(included) != set(RECOVERY_COMPONENTS)
        ):
            raise BackupError("Recovery manifest components are invalid")
        return value

    def create(self, passphrase: str) -> dict[str, Any]:
        self._require_passphrase(passphrase)
        if not self.database_path.is_file():
            raise BackupError("SQLite database does not exist")
        created_at = _now()
        name = f"pixora-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3.enc"
        encrypted = self.backup_dir / name
        with tempfile.TemporaryDirectory(dir=self.backup_dir) as temporary_dir:
            plain = Path(temporary_dir) / "snapshot.sqlite3"
            self._sqlite_snapshot(plain)
            self._openssl("-salt", "-in", str(plain), "-out", str(encrypted), passphrase=passphrase)
        digest = self._sha256(encrypted)
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

    def create_recovery_bundle(self, passphrase: str) -> dict[str, Any]:
        """Create an encrypted DB + private-storage bundle without secrets."""

        self._require_passphrase(passphrase)
        if not self.database_path.is_file():
            raise BackupError("SQLite database does not exist")
        source_revision = self._source_revision()
        if source_revision is None:
            raise BackupError("Deployed source revision is unavailable")
        created_at = _now()
        name = f"ravuna-recovery-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}.tar.gz.enc"
        encrypted = self.backup_dir / name
        with tempfile.TemporaryDirectory(dir=self.backup_dir) as temporary_dir:
            temporary = Path(temporary_dir)
            staging = temporary / "staging"
            database_dir = staging / "database"
            private_storage = staging / "private_storage" / "users"
            database_dir.mkdir(parents=True)
            private_storage.parent.mkdir(parents=True)
            snapshot = database_dir / "photo_bot.sqlite3"
            sqlite_status = self._sqlite_snapshot(snapshot)
            if self.users_dir.is_dir():
                shutil.copytree(self.users_dir, private_storage, symlinks=True)
            else:
                private_storage.mkdir()

            file_hashes: dict[str, str] = {}
            storage_bytes = 0
            storage_files = 0
            for candidate in sorted(private_storage.rglob("*")):
                if candidate.is_symlink():
                    raise BackupError("Private storage contains an unsupported symlink")
                if candidate.is_file():
                    relative = candidate.relative_to(staging).as_posix()
                    file_hashes[relative] = self._sha256(candidate)
                    storage_bytes += candidate.stat().st_size
                    storage_files += 1
            file_hashes["database/photo_bot.sqlite3"] = self._sha256(snapshot)
            manifest = {
                "format": "ravuna-recovery-v1",
                "bundle_version": RECOVERY_BUNDLE_VERSION,
                "created_at": created_at.isoformat(),
                "source_revision": source_revision,
                "included_components": list(RECOVERY_COMPONENTS),
                "encrypted": True,
                "sqlite": sqlite_status,
                "private_storage": {
                    "file_count": storage_files,
                    "size_bytes": storage_bytes,
                },
                "sha256": file_hashes,
                "secrets_included": False,
                "external_dependencies": [
                    "Git repository at the deployed revision",
                    "approved production secret storage",
                    "host, systemd, nginx and TLS configuration",
                ],
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )
            plain_archive = temporary / "recovery.tar.gz"
            with tarfile.open(plain_archive, "w:gz") as archive:
                archive.add(staging / "manifest.json", arcname="manifest.json")
                archive.add(database_dir, arcname="database")
                archive.add(staging / "private_storage", arcname="private_storage")
            self._openssl(
                "-salt", "-in", str(plain_archive), "-out", str(encrypted),
                passphrase=passphrase,
            )
        try:
            encrypted.chmod(0o600)
        except OSError:
            pass
        metadata = {
            "recovery_bundle_name": name,
            "created_at": created_at.isoformat(),
            "encrypted": True,
            "size_bytes": encrypted.stat().st_size,
            "sha256": self._sha256(encrypted),
            "bundle_version": RECOVERY_BUNDLE_VERSION,
            "source_revision": source_revision,
            "included_components": list(RECOVERY_COMPONENTS),
            "secrets_included": False,
        }
        _write_json(self.backup_dir / "latest_recovery.json", metadata)
        self.prune(created_at)
        return metadata

    def _safe_restore_root(self, restore_root: Path) -> Path:
        candidate = restore_root.resolve()
        home = Path.home().resolve()
        anchor = Path(candidate.anchor).resolve()
        if candidate in {anchor, home, self.base_dir}:
            raise BackupError("Restore root is unsafe")
        if candidate in self.base_dir.parents or self.base_dir in candidate.parents:
            raise BackupError("Restore root must be separate from the application root")
        if candidate.exists() and any(candidate.iterdir()):
            raise BackupError("Restore root must be absent or empty")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    @staticmethod
    def _extract_archive(archive_path: Path, destination: Path) -> None:
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                members = archive.getmembers()
                for member in members:
                    member_path = Path(member.name)
                    if (
                        member_path.is_absolute()
                        or ".." in member_path.parts
                        or member.issym()
                        or member.islnk()
                    ):
                        raise BackupError("Recovery archive contains an unsafe member")
                archive.extractall(destination, members=members, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise BackupError("Recovery archive integrity check failed") from exc

    def restore_recovery_bundle(
        self,
        backup: Path,
        passphrase: str,
        *,
        restore_root: Path | None = None,
    ) -> dict[str, Any]:
        """Restore and validate a recovery bundle outside the application root."""

        self._require_passphrase(passphrase)
        if backup.is_absolute() or backup.name != str(backup):
            raise BackupError("Backup must be referenced by file name")
        candidate = self.backup_dir / backup.name
        if not candidate.is_file() or not candidate.name.startswith("ravuna-recovery-"):
            raise BackupError("Encrypted recovery bundle was not found")

        temporary_context: tempfile.TemporaryDirectory[str] | None = None
        if restore_root is None:
            temporary_context = tempfile.TemporaryDirectory()
            destination = Path(temporary_context.name) / "restore"
        else:
            destination = self._safe_restore_root(restore_root)
        destination.mkdir(parents=True, exist_ok=True)
        backup_timestamp: object = None
        artifact_status = "FAIL"
        sqlite_report_status = "NOT_CHECKED"
        missing: list[str] = []
        try:
            try:
                with tempfile.TemporaryDirectory() as temporary_dir:
                    plain_archive = Path(temporary_dir) / "recovery.tar.gz"
                    self._openssl(
                        "-d", "-in", str(candidate), "-out", str(plain_archive),
                        passphrase=passphrase,
                    )
                    self._extract_archive(plain_archive, destination)

                manifest_path = destination / "manifest.json"
                for relative in ("manifest.json", *RECOVERY_COMPONENTS):
                    if not (destination / relative).exists():
                        missing.append(relative)
                if missing:
                    raise BackupError("Recovery bundle is missing required components")
                try:
                    manifest_value = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    raise BackupError("Recovery manifest is invalid") from exc
                manifest = self._validate_recovery_manifest(manifest_value)
                backup_timestamp = manifest.get("created_at")
                if manifest.get("format") != "ravuna-recovery-v1":
                    raise BackupError("Recovery manifest format is unsupported")
                expected_hashes = manifest.get("sha256")
                if not isinstance(expected_hashes, dict):
                    raise BackupError("Recovery manifest hashes are invalid")
                for relative, expected in expected_hashes.items():
                    path = destination / str(relative)
                    if not path.is_file() or not isinstance(expected, str):
                        raise BackupError("Recovery component is missing")
                    if self._sha256(path) != expected:
                        raise BackupError("Recovery component integrity check failed")
                artifact_status = "PASS"
                sqlite_status = self._validate_sqlite(
                    destination / "database" / "photo_bot.sqlite3"
                )
                sqlite_report_status = "PASS"
                report = {
                    "backup_timestamp": backup_timestamp,
                    "artifact_status": artifact_status,
                    "sqlite_status": sqlite_report_status,
                    "migration": sqlite_status["migration"],
                    "source_revision": manifest.get("source_revision"),
                    "restore_status": "PASS",
                    "missing_components": [],
                    "overall": "PASS",
                    "backup_name": candidate.name,
                    "tested_at": _now().isoformat(),
                }
            except BackupError as exc:
                report = {
                    "backup_timestamp": backup_timestamp,
                    "artifact_status": artifact_status,
                    "sqlite_status": sqlite_report_status,
                    "migration": None,
                    "source_revision": None,
                    "restore_status": "FAIL",
                    "missing_components": missing,
                    "overall": "FAIL",
                    "backup_name": candidate.name,
                    "tested_at": _now().isoformat(),
                    "error": str(exc),
                }
            _write_json(self.backup_dir / "recovery_restore_status.json", report)
            return report
        finally:
            if temporary_context is not None:
                temporary_context.cleanup()

    def mark_recovery_offsite(self, backup_name: str, provider: str) -> dict[str, Any]:
        candidate = self.backup_dir / Path(backup_name).name
        if not candidate.is_file() or not candidate.name.startswith("ravuna-recovery-"):
            raise BackupError("Recovery bundle to mark off-site was not found")
        report = {
            "backup_name": candidate.name,
            "provider": provider,
            "copied_at": _now().isoformat(),
        }
        _write_json(self.backup_dir / "recovery_offsite_status.json", report)
        return report

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
        candidates = (
            *self.backup_dir.glob("pixora-*.sqlite3.enc"),
            *self.backup_dir.glob("ravuna-recovery-*.tar.gz.enc"),
        )
        for candidate in candidates:
            if candidate.is_symlink():
                continue
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                candidate.unlink()
                removed += 1
        return removed
