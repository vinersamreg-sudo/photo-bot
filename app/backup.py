"""Encrypted, restore-tested SQLite backups for Ravuna operations.

The ``pixora-`` filename prefix is retained as an immutable backup-format
identifier so existing encrypted backup rotation and restore discovery remain
compatible across the brand migration.
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from app.recovery_retention import (
    RetentionError, artifact, read_json, validate, write_stage, prune as recovery_prune,
)


RECOVERY_BUNDLE_VERSION = 1
RESTORE_FREE_SPACE_MARGIN = 64 * 1024 * 1024
DAILY_FREE_SPACE_MARGIN = 2 * 1024**3
DAILY_BACKUP_BUDGET = 16 * 1024**3
RECOVERY_COMPONENTS = (
    "database/photo_bot.sqlite3",
    "private_storage/users",
)
COMMERCE_TABLES = (
    "payment_orders", "user_credit_accounts", "generation_credit_lots",
    "generation_credit_reservations", "credit_ledger", "unlock_entitlements",
    "continuation_pack_grants", "referral_bonus_grants",
)


class BackupError(RuntimeError):
    """A safe operational backup failure without secret material."""


def _os_reason(error: OSError) -> str:
    return {
        errno.ENOSPC: "no_space_left",
        errno.EDQUOT: "disk_quota_exceeded",
        errno.EACCES: "permission_denied",
        errno.EPERM: "permission_denied",
        errno.EIO: "io_error",
        errno.ENOENT: "source_missing",
    }.get(error.errno, "io_failure")


def _openssl_reason(returncode: int, stderr: str | bytes) -> str:
    # Raw stderr can contain secrets or private filenames. It is never returned.
    text = (stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes)
            else stderr or "")[:65536].lower()
    if returncode < 0:
        return "openssl_killed"
    for fragments, reason in (
        (("no space left on device",), "no_space_left"),
        (("disk quota exceeded",), "disk_quota_exceeded"),
        (("permission denied",), "permission_denied"),
        (("input/output error", "i/o error"), "io_error"),
        (("bad decrypt", "bad magic number"), "invalid_passphrase_or_artifact"),
    ):
        if any(fragment in text for fragment in fragments):
            return reason
    return "openssl_failed"


class _HashingReader:
    """Hash exactly the bytes placed in the tar member, not a second file read."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        value = self.stream.read(size)
        self.digest.update(value)
        self.size += len(value)
        return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
                input=(passphrase + "\n").encode("utf-8"),
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            reason = "openssl_unavailable" if exc.errno == errno.ENOENT else _os_reason(exc)
            raise BackupError(f"OpenSSL is unavailable (reason={reason})") from None
        if result.returncode != 0:
            reason = _openssl_reason(result.returncode, result.stderr)
            raise BackupError(
                f"Backup encryption operation failed (reason={reason}; exit_code={result.returncode})"
            )

    @staticmethod
    @contextmanager
    def _openssl_stream(
        path: Path, *, passphrase: str, decrypt: bool = False,
    ) -> Iterator[BinaryIO]:
        """Stream compressed archive bytes; keep passphrase on a separate channel."""

        read_fd = write_fd = None
        process = None
        try:
            options: dict[str, Any] = {}
            if os.name == "posix":
                read_fd, write_fd = os.pipe()
                pass_args = ["-pass", f"fd:{read_fd}"]
                options["pass_fds"] = (read_fd,)
            else:
                # Windows has no pass_fds. Child-only environment, never argv or
                # the parent's environment; production uses the private POSIX fd.
                options["env"] = {**os.environ, "RAVUNA_BACKUP_STREAM_PASSWORD": passphrase}
                pass_args = ["-pass", "env:RAVUNA_BACKUP_STREAM_PASSWORD"]
            with tempfile.TemporaryFile() as diagnostics:
                try:
                    process = subprocess.Popen(
                        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                         *(["-d", "-in", str(path)] if decrypt else ["-salt", "-out", str(path)]),
                         *pass_args],
                        stdin=subprocess.DEVNULL if decrypt else subprocess.PIPE,
                        stdout=subprocess.PIPE if decrypt else subprocess.DEVNULL,
                        stderr=diagnostics, **options,
                    )
                except OSError as exc:
                    reason = "openssl_unavailable" if exc.errno == errno.ENOENT else _os_reason(exc)
                    raise BackupError(f"OpenSSL is unavailable (reason={reason})") from None
                if read_fd is not None:
                    os.close(read_fd)
                    read_fd = None
                if write_fd is not None:
                    password_fd = write_fd
                    write_fd = None
                    with os.fdopen(password_fd, "wb") as secret_input:
                        secret_input.write((passphrase + "\n").encode("utf-8"))
                stream = process.stdout if decrypt else process.stdin
                assert stream is not None
                try:
                    yield stream
                    if decrypt:
                        # Drain gzip/tar padding so OpenSSL can validate final
                        # cipher padding and exit; never accept an early tar EOF.
                        while stream.read(1024 * 1024):
                            pass
                    stream.close()
                except BaseException as exc:
                    original_code = process.poll()
                    killed_for_cleanup = False
                    if decrypt and isinstance(exc, (tarfile.TarError, BackupError)):
                        # A parser may reject decrypted bytes before OpenSSL
                        # finishes padding validation. Drain, without keeping
                        # plaintext, before collecting its true result.
                        while stream.read(1024 * 1024):
                            pass
                    if original_code is None and isinstance(exc, (BrokenPipeError, tarfile.ReadError, BackupError)):
                        try:
                            original_code = process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                    if process.poll() is None:
                        killed_for_cleanup = True
                        process.terminate()
                    try:
                        stream.close()
                    except OSError:
                        pass
                    code = process.wait(timeout=30)
                    if isinstance(exc, (BrokenPipeError, tarfile.ReadError, BackupError)) and code != 0 and not killed_for_cleanup:
                        diagnostics.seek(0)
                        reason = _openssl_reason(code, diagnostics.read(65536))
                        raise BackupError(
                            f"Backup encryption operation failed (reason={reason}; exit_code={code})"
                        ) from None
                    raise
                code = process.wait()
                if code != 0:
                    diagnostics.seek(0)
                    reason = _openssl_reason(code, diagnostics.read(65536))
                    raise BackupError(
                        f"Backup encryption operation failed (reason={reason}; exit_code={code})"
                    )
        finally:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        try:
                            stream.close()
                        except OSError:
                            pass

    @staticmethod
    def _validate_sqlite(path: Path) -> dict[str, Any]:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                migration = connection.execute(
                    "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
                ).fetchone()
                foreign_key_violation = connection.execute("PRAGMA foreign_key_check").fetchone()
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                commerce = {}
                for table in COMMERCE_TABLES:
                    if table not in tables:  # Legacy schemas remain restorable.
                        continue
                    digest, count = hashlib.sha256(), 0
                    for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                        encoded = json.dumps(list(row), ensure_ascii=False, default=str,
                                             separators=(",", ":")).encode("utf-8")
                        digest.update(len(encoded).to_bytes(8, "big"))
                        digest.update(encoded)
                        count += 1
                    commerce[table] = {"count": count, "sha256": digest.hexdigest()}
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise BackupError("Restored SQLite database could not be opened") from exc
        if not quick_check or quick_check[0] != "ok":
            raise BackupError("Restored SQLite integrity check failed")
        if foreign_key_violation is not None:
            raise BackupError("Restored SQLite foreign key check failed")
        return {"quick_check": "ok", "foreign_key_check": "ok",
                "migration": int(migration[0]), "commerce_fingerprints": commerce}

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _validate_gallery_files(
        self, database: Path, restored_users: Path,
        verified: set[str] | None = None,
    ) -> dict[str, int]:
        """Check retained originals/previews without selecting customer text."""
        counts = {"originals": 0, "previews": 0}
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        rows = None
        try:
            connection.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"gallery_versions", "gallery_items"} <= tables:
                return counts
            columns = {row[1] for row in connection.execute("PRAGMA table_info(gallery_items)")}
            predicate = "WHERE i.purged_at IS NULL" if "purged_at" in columns else ""
            rows = connection.execute(
                "SELECT v.original_path,v.preview_watermarked_path FROM gallery_versions v "
                "JOIN gallery_items i ON i.id=v.gallery_item_id " + predicate
            )
            for row in rows:
                for key, stored in zip(counts, row):
                    if not stored:
                        continue
                    source = Path(stored)
                    try:
                        relative = source.relative_to(self.users_dir)
                    except ValueError:
                        raise BackupError("Gallery file reference is outside private storage") from None
                    candidate = restored_users / relative
                    if ".." in relative.parts or (
                        relative.as_posix() not in verified if verified is not None else
                        not candidate.is_file() or candidate.is_symlink()
                        or not candidate.resolve().is_relative_to(restored_users.resolve())
                    ):
                        raise BackupError("Retained gallery file is missing or unsafe in recovery")
                    counts[key] += 1
            return counts
        finally:
            # An early validation error leaves a live cursor in its traceback.
            # Finalize it before cleaning the decrypted scratch DB on Windows.
            if rows is not None:
                rows.close()
            connection.close()

    def _sqlite_snapshot(self, destination: Path, *, standalone: bool = False) -> dict[str, Any]:
        source = sqlite3.connect(self.database_path.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            if standalone and target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise BackupError("SQLite recovery snapshot must not require WAL sidecars")
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

    def create(self, passphrase: str, *, _standalone_sqlite: bool = False) -> dict[str, Any]:
        self._require_passphrase(passphrase)
        if not self.database_path.is_file():
            raise BackupError("SQLite database does not exist")
        created_at = _now()
        name = f"pixora-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3.enc"
        encrypted = self.backup_dir / name
        with tempfile.TemporaryDirectory(dir=self.backup_dir) as temporary_dir:
            plain = Path(temporary_dir) / "snapshot.sqlite3"
            self._sqlite_snapshot(plain, standalone=_standalone_sqlite)
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
        self.prune_sqlite_by_age(created_at)
        return metadata

    def daily_capacity(self, total_backup_bytes: int) -> dict[str, int]:
        """Bound the daily cycle without reserving space for a full plaintext tree."""
        db = self.database_path.stat().st_size
        # Account for SQLite growth from a concurrent WAL plus modest headroom.
        wal = self.database_path.with_name(self.database_path.name + "-wal")
        db += (wal.stat().st_size if wal.exists() else 0) + 16 * 1024**2
        archive, largest = db + 1024**2, db
        if self.users_dir.is_symlink():
            raise BackupError("Private storage contains an unsupported symlink")
        for path in self.users_dir.rglob("*"):
            entry = path.lstat()
            if not (stat.S_ISREG(entry.st_mode) or stat.S_ISDIR(entry.st_mode)):
                raise BackupError("Private storage contains an unsupported entry")
            if stat.S_ISREG(entry.st_mode):
                largest = max(largest, entry.st_size)
                archive += entry.st_size
            # tar/PAX headers, padding and manifest entries.
            archive += 8192 + 4 * len(os.fsencode(path))
        archive += archive // 100
        scratch = 2 * db + largest + 16 * 1024**2
        required = archive + scratch + DAILY_FREE_SPACE_MARGIN
        if total_backup_bytes + archive + scratch > DAILY_BACKUP_BUDGET:
            raise BackupError("Daily recovery capacity preflight failed (reason=backup_budget)")
        if shutil.disk_usage(self.backup_dir).free < required:
            raise BackupError("Daily recovery capacity preflight failed (reason=no_space_left)")
        return {"ciphertext_limit": archive, "scratch_limit": scratch,
                "member_limit": max(largest, 16 * 1024**2), "required_free": required}

    def create_recovery_bundle(
        self, passphrase: str, *, daily_ciphertext_limit: int | None = None,
        _expanded_byte_cap: int | None = None,
        _standalone_sqlite: bool = False,
    ) -> dict[str, Any]:
        """Create an encrypted DB + private-storage bundle without secrets."""

        self._require_passphrase(passphrase)
        if not self.database_path.is_file():
            raise BackupError("SQLite database does not exist")
        source_revision = self._source_revision()
        if source_revision is None:
            raise BackupError("Deployed source revision is unavailable")
        # Manual workflows still reserve for an assembled full restore.
        logical_bytes = self.database_path.stat().st_size
        try:
            if self.users_dir.is_symlink():
                raise BackupError("Private storage contains an unsupported symlink")
            for path in self.users_dir.rglob("*"):
                entry = path.lstat()
                if stat.S_ISLNK(entry.st_mode):
                    raise BackupError("Private storage contains an unsupported symlink")
                logical_bytes += 4096 + (entry.st_size if stat.S_ISREG(entry.st_mode) else 0)
            if _expanded_byte_cap is not None and logical_bytes + 64 * 1024**2 > _expanded_byte_cap:
                raise BackupError("Recovery input exceeds its expanded byte cap")
            required = (2 * (logical_bytes + logical_bytes // 100) + RESTORE_FREE_SPACE_MARGIN
                        if daily_ciphertext_limit is None else
                        daily_ciphertext_limit + DAILY_FREE_SPACE_MARGIN)
            if shutil.disk_usage(self.backup_dir).free < required:
                raise BackupError("Recovery capacity preflight failed (reason=no_space_left)")
        except OSError as exc:
            raise BackupError(f"Recovery capacity preflight failed (reason={_os_reason(exc)})") from None
        created_at = _now()
        name = f"ravuna-recovery-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}.tar.gz.enc"
        encrypted = self.backup_dir / name
        try:
            with tempfile.TemporaryDirectory(dir=self.backup_dir) as temporary_dir:
                temporary = Path(temporary_dir)
                snapshot = temporary / "snapshot.sqlite3"
                sqlite_status = self._sqlite_snapshot(snapshot, standalone=_standalone_sqlite)
                snapshot.chmod(0o600)
                partial = temporary / "bundle.enc"
                # Mode is private before the first encrypted byte is written.
                with partial.open("xb"):
                    pass
                partial.chmod(0o600)
                with self._openssl_stream(partial, passphrase=passphrase) as stream:
                    self._write_recovery_archive(
                        stream, snapshot, sqlite_status, created_at, source_revision,
                        byte_cap=_expanded_byte_cap,
                    )
                if daily_ciphertext_limit is not None and partial.stat().st_size > daily_ciphertext_limit:
                    raise BackupError("Daily recovery ciphertext exceeded its budget")
                # Publish only a complete encrypted artifact. Failure leaves no
                # plaintext archive, copied media or discoverable partial bundle.
                partial.replace(encrypted)
        except OSError as exc:
            raise BackupError(f"Recovery bundle creation failed (reason={_os_reason(exc)})") from None
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
        self._recovery_stage(name, "creation", metadata)
        self.prune_sqlite_by_age(created_at)
        return metadata

    def _recovery_stage(self, name: str, stage: str, value: dict[str, Any]) -> None:
        try:
            write_stage(self.backup_dir, name, stage, value)
        except (RetentionError, OSError, ValueError, TypeError):
            raise BackupError("Recovery evidence could not be recorded safely") from None

    def _write_recovery_archive(
        self, stream: BinaryIO, snapshot: Path, sqlite_status: dict[str, Any],
        created_at: datetime, source_revision: str, *, byte_cap: int | None = None,
    ) -> None:
        file_hashes = {"database/photo_bot.sqlite3": self._sha256(snapshot)}
        storage_bytes = storage_files = 0
        logical = snapshot.stat().st_size + 64 * 1024**2
        if self.users_dir.is_symlink():
            raise BackupError("Private storage contains an unsupported symlink")
        users_root = self.users_dir.resolve()
        with tarfile.open(fileobj=stream, mode="w|gz") as archive:
            archive.add(snapshot, arcname="database/photo_bot.sqlite3", recursive=False)
            for name in ("private_storage", "private_storage/users"):
                member = tarfile.TarInfo(name)
                member.type, member.mode = tarfile.DIRTYPE, 0o700
                archive.addfile(member)
            for candidate in sorted(self.users_dir.rglob("*")):
                before = candidate.lstat()
                logical += 4096 + (before.st_size if stat.S_ISREG(before.st_mode) else 0)
                if byte_cap is not None and logical > byte_cap:
                    raise BackupError("Recovery input grew beyond its expanded byte cap")
                if stat.S_ISLNK(before.st_mode):
                    raise BackupError("Private storage contains an unsupported symlink")
                if not candidate.resolve().is_relative_to(users_root):
                    raise BackupError("Private storage path is unsafe")
                relative = "private_storage/users/" + candidate.relative_to(self.users_dir).as_posix()
                member = tarfile.TarInfo(relative)
                member.mode, member.mtime = 0o600, before.st_mtime
                if stat.S_ISDIR(before.st_mode):
                    member.type, member.mode = tarfile.DIRTYPE, 0o700
                    archive.addfile(member)
                    continue
                if not stat.S_ISREG(before.st_mode):
                    raise BackupError("Private storage contains an unsupported file type")
                descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as source:
                    opened = os.fstat(source.fileno())
                    def identity(value: os.stat_result) -> tuple[int, ...]:
                        # POSIX ctime tracks changes; Windows ctime is creation
                        # time, not a content-change signal. Never drop it on VPS.
                        common = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
                        return common + ((value.st_ctime_ns,) if os.name == "posix" else ())
                    if not stat.S_ISREG(opened.st_mode) or identity(before) != identity(opened):
                        raise BackupError("Recovery bundle creation failed (reason=private_storage_changed)")
                    reader = _HashingReader(source)
                    member.size = opened.st_size
                    archive.addfile(member, reader)
                    if reader.size != opened.st_size or identity(os.fstat(source.fileno())) != identity(opened):
                        raise BackupError("Recovery bundle creation failed (reason=private_storage_changed)")
                    file_hashes[relative] = reader.digest.hexdigest()
                    storage_bytes += reader.size
                    storage_files += 1
            manifest = {
                "format": "ravuna-recovery-v1", "bundle_version": RECOVERY_BUNDLE_VERSION,
                "created_at": created_at.isoformat(), "source_revision": source_revision,
                "included_components": list(RECOVERY_COMPONENTS), "encrypted": True,
                "sqlite": sqlite_status,
                "private_storage": {"file_count": storage_files, "size_bytes": storage_bytes},
                "sha256": file_hashes, "secrets_included": False,
                "external_dependencies": ["Git repository at the deployed revision",
                                          "approved production secret storage",
                                          "host, systemd, nginx and TLS configuration"],
            }
            # Hashes describe the bytes actually archived. Manifest may be last:
            # v1 restore does not depend on archive-member ordering.
            contents = json.dumps(manifest, sort_keys=True).encode("utf-8")
            member = tarfile.TarInfo("manifest.json")
            member.mode, member.size = 0o600, len(contents)
            archive.addfile(member, io.BytesIO(contents))

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
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate.chmod(0o700)
        return candidate

    @staticmethod
    def _extract_recovery_stream(stream: BinaryIO, destination: Path, *, byte_cap: int | None = None) -> None:
        try:
            # tarfile's r|gz decompressor does not validate gzip CRC/trailer.
            # Keep the original archive-integrity guarantee while streaming.
            total = 0
            with gzip.GzipFile(fileobj=stream, mode="rb") as expanded:
                with tarfile.open(fileobj=expanded, mode="r|") as archive:
                    for member in archive:
                        member_path = Path(member.name)
                        if (
                            member_path.is_absolute() or ".." in member_path.parts
                            or member.issym() or member.islnk()
                            or not (member.isfile() or member.isdir())
                        ):
                            raise BackupError("Recovery archive contains an unsafe member")
                        total += max(4096, member.size + 4096)
                        if byte_cap is not None and total > byte_cap:
                            raise BackupError("Recovery expansion exceeds scratch budget")
                        if member.isfile() and shutil.disk_usage(destination).free < (
                            member.size + RESTORE_FREE_SPACE_MARGIN
                        ):
                            # Leave space for the failure receipt and the live
                            # application; an unsuccessful restore must not fill
                            # the filesystem before its diagnostics are persisted.
                            raise BackupError("Recovery archive extraction failed (reason=no_space_left)")
                        archive.extract(member, destination, filter="data")
                while expanded.read(1024 * 1024):
                    pass  # Require gzip trailer/CRC validation, not just tar EOF.
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            if isinstance(exc, OSError) and not isinstance(exc, gzip.BadGzipFile):
                raise BackupError(f"Recovery archive extraction failed (reason={_os_reason(exc)})") from None
            raise BackupError("Recovery archive integrity check failed") from None

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

    @classmethod
    def _restore_one_at_a_time(
        cls, stream: BinaryIO, destination: Path, member_limit: int,
    ) -> dict[str, str]:
        """Materialize and reread every file, retaining only the SQLite and manifest."""
        hashes: dict[str, str] = {}
        seen: set[str] = set()
        scratch = destination / ".media-restore"
        manifest = "manifest.json"
        database, users = RECOVERY_COMPONENTS
        try:
            with gzip.GzipFile(fileobj=stream, mode="rb") as expanded:
                with tarfile.open(fileobj=expanded, mode="r|") as archive:
                    for member in archive:
                        name = member.name
                        parts = name.split("/")
                        if (name in seen or any(p in {"", ".", ".."} for p in parts)
                                or "\\" in name or ":" in name
                                or not (member.isfile() or member.isdir())):
                            raise BackupError("Recovery archive contains an unsafe member")
                        seen.add(name)
                        if member.isdir():
                            if name not in {"private_storage", users} and not name.startswith(users + "/"):
                                raise BackupError("Recovery archive contains an unexpected directory")
                            continue
                        if name not in {manifest, database} and not name.startswith(users + "/"):
                            raise BackupError("Recovery archive contains an unexpected file")
                        if (member.size > member_limit
                                or (name == manifest and member.size > 64 * 1024**2)
                                or shutil.disk_usage(destination).free < member.size + DAILY_FREE_SPACE_MARGIN):
                            raise BackupError("Daily recovery restore exceeded its scratch budget")
                        path = destination / name if name in {database, manifest} else scratch
                        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        source = archive.extractfile(member)
                        if source is None:
                            raise BackupError("Recovery member is unreadable")
                        try:
                            with source, path.open("xb") as output:
                                shutil.copyfileobj(source, output, length=1024**2)
                            if name != manifest:
                                hashes[name] = cls._sha256(path)
                        finally:
                            if path == scratch:
                                scratch.unlink(missing_ok=True)
                while expanded.read(1024**2):
                    pass
            if not {"private_storage", users, database, manifest} <= seen:
                raise BackupError("Recovery bundle is missing required components")
            # Detect incompatible tree layouts even though media use one scratch file.
            if any("/".join(name.split("/")[:i]) in hashes
                   for name in seen for i in range(1, len(name.split("/")))):
                raise BackupError("Recovery archive has incompatible paths")
            return hashes
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            reason = _os_reason(exc) if isinstance(exc, OSError) else "archive_integrity"
            raise BackupError(f"Daily recovery restore failed (reason={reason})") from None

    def restore_recovery_bundle(
        self,
        backup: Path,
        passphrase: str,
        *,
        restore_root: Path | None = None,
        _bounded_member_limit: int | None = None,
        _scratch_parent: Path | None = None,
        _expanded_byte_cap: int | None = None,
    ) -> dict[str, Any]:
        """Restore and validate a recovery bundle outside the application root."""

        self._require_passphrase(passphrase)
        if backup.is_absolute() or backup.name != str(backup):
            raise BackupError("Backup must be referenced by file name")
        candidate = self.backup_dir / backup.name
        if not candidate.is_file() or not candidate.name.startswith("ravuna-recovery-"):
            raise BackupError("Encrypted recovery bundle was not found")
        try:
            encrypted_before = artifact(candidate)
        except (RetentionError, OSError):
            raise BackupError("Encrypted recovery bundle is unsafe") from None

        temporary_context: tempfile.TemporaryDirectory[str] | None = None
        if restore_root is None:
            temporary_context = tempfile.TemporaryDirectory(dir=_scratch_parent)
            destination = Path(temporary_context.name) / "restore"
        else:
            destination = self._safe_restore_root(restore_root)
        destination.mkdir(parents=True, exist_ok=True)
        backup_timestamp: object = None
        artifact_status = "FAIL"
        sqlite_report_status = "NOT_CHECKED"
        missing: list[str] = []
        restore_passed = False
        try:
            try:
                restored_hashes = None
                with self._openssl_stream(candidate, passphrase=passphrase, decrypt=True) as stream:
                    if _bounded_member_limit is None:
                        self._extract_recovery_stream(stream, destination, byte_cap=_expanded_byte_cap)
                    else:
                        restored_hashes = self._restore_one_at_a_time(
                            stream, destination, _bounded_member_limit,
                        )

                manifest_path = destination / "manifest.json"
                for relative in ("manifest.json", *RECOVERY_COMPONENTS):
                    if relative == RECOVERY_COMPONENTS[1] and restored_hashes is not None:
                        continue
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
                if restored_hashes is not None and restored_hashes != expected_hashes:
                    raise BackupError("Recovery component integrity check failed")
                for relative, expected in (expected_hashes.items() if restored_hashes is None else ()):
                    path = destination / str(relative)
                    if not path.is_file() or not isinstance(expected, str):
                        raise BackupError("Recovery component is missing")
                    if self._sha256(path) != expected:
                        raise BackupError("Recovery component integrity check failed")
                artifact_status = "PASS"
                sqlite_status = self._validate_sqlite(
                    destination / "database" / "photo_bot.sqlite3"
                )
                manifest_sqlite = manifest.get("sqlite")
                if not isinstance(manifest_sqlite, dict):
                    raise BackupError("Recovery SQLite manifest is invalid")
                expected_commerce = manifest_sqlite.get("commerce_fingerprints")
                if (expected_commerce is not None
                        and expected_commerce != sqlite_status["commerce_fingerprints"]):
                    raise BackupError("Restored SQLite commerce fingerprint check failed")
                gallery_files = self._validate_gallery_files(
                    destination / "database" / "photo_bot.sqlite3",
                    destination / "private_storage" / "users",
                    None if restored_hashes is None else {
                        name.removeprefix(RECOVERY_COMPONENTS[1] + "/")
                        for name in restored_hashes if name.startswith(RECOVERY_COMPONENTS[1] + "/")
                    },
                )
                sqlite_report_status = "PASS"
                report = {
                    "backup_timestamp": backup_timestamp,
                    "artifact_status": artifact_status,
                    "sqlite_status": sqlite_report_status,
                    "migration": sqlite_status["migration"],
                    "foreign_key_check": sqlite_status["foreign_key_check"],
                    "commerce_fingerprints": sqlite_status["commerce_fingerprints"],
                    "gallery_files": gallery_files,
                    "source_revision": manifest.get("source_revision"),
                    "restore_status": "PASS",
                    "restore_method": "assembled_tree" if restored_hashes is None else "bounded_file_restore",
                    "missing_components": [],
                    "overall": "PASS",
                    "backup_name": candidate.name,
                    "tested_at": _now().isoformat(),
                }
                try:
                    if artifact(candidate) != encrypted_before:
                        raise RetentionError("changed")
                except (RetentionError, OSError):
                    raise BackupError("Recovery artifact changed during restore proof") from None
                report["sha256"] = encrypted_before["sha256"]
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
                # In particular on ENOSPC, release isolated plaintext BEFORE
                # writing the FAIL receipt. Otherwise even the diagnostic write
                # can fail, leaving yesterday's PASS and no per-artifact proof.
                shutil.rmtree(destination)
            _write_json(self.backup_dir / "recovery_restore_status.json", report)
            self._recovery_stage(candidate.name, "restore", report)
            restore_passed = report.get("overall") == "PASS"
            return report
        finally:
            if temporary_context is not None:
                temporary_context.cleanup()
            elif not restore_passed and destination.exists():
                # The caller explicitly supplied an initially empty isolated
                # restore root, checked above. Never retain failed plaintext.
                shutil.rmtree(destination)

    def mark_recovery_offsite(self, backup_name: str, provider: str) -> dict[str, Any]:
        if Path(backup_name).name != backup_name or not provider:
            raise BackupError("Recovery off-site confirmation is invalid")
        candidate = self.backup_dir / backup_name
        try:
            current = artifact(candidate)
            proof = read_json(self.backup_dir / (backup_name + ".proof.json"))
            validate(proof, backup_name, current, offsite=False)
        except (RetentionError, OSError, ValueError, TypeError):
            raise BackupError("Recovery creation/restore proof is required before off-site confirmation") from None
        report = {
            "backup_name": candidate.name,
            "provider": provider,
            "copied_at": _now().isoformat(),
            "overall": "PASS",
            "sha256": current["sha256"],
        }
        self._recovery_stage(candidate.name, "offsite", report)
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
        """Compatibility alias: age-based retention applies ONLY to SQLite."""
        return self.prune_sqlite_by_age(now)

    def prune_sqlite_by_age(self, now: datetime | None = None) -> int:
        cutoff = (now or _now()) - timedelta(days=self.retention_days)
        removed = 0
        candidates = self.backup_dir.glob("pixora-*.sqlite3.enc")
        for candidate in candidates:
            if candidate.is_symlink():
                continue
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                candidate.unlink()
                removed += 1
        return removed

    def prune_recovery_by_count(
        self, keep: int = 7, *, confirmed_backup: str, dry_run: bool = True,
    ) -> dict[str, Any]:
        try:
            return recovery_prune(self.backup_dir, keep=keep,
                                  confirmed_backup=confirmed_backup, dry_run=dry_run)
        except (RetentionError, OSError, ValueError, TypeError):
            raise BackupError("Recovery retention blocked: evidence/artifacts are unproven or unsafe") from None
