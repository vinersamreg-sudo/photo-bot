import errno
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.backup import (
    BackupError, BackupManager, RESTORE_FREE_SPACE_MARGIN,
    _HashingReader, _openssl_reason, _write_json,
)
from app.database import Database


SECRET = "synthetic-test-passphrase-only"


@contextmanager
def plain_stream(path, *, passphrase, decrypt=False):
    with path.open("rb" if decrypt else "wb") as stream:
        yield stream


class BackupStreamingTests(TestCase):
    def test_atomic_json_failure_preserves_previous_receipt_without_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text('{"previous":"PASS"}')
            with patch("app.backup.json.dump", side_effect=OSError(errno.ENOSPC, SECRET)):
                with self.assertRaises(OSError):
                    _write_json(path, {"overall": "FAIL"})
            self.assertEqual(path.read_text(), '{"previous":"PASS"}')
            self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_failed_restore_releases_plaintext_before_publishing_failure_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, _ = self.setup_manager(root)
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                created = manager.create_recovery_bundle(SECRET)
            destination = root / "isolated"

            def exhausted(stream, target):
                (target / "partial-private-file").write_bytes(b"synthetic")
                raise BackupError("Recovery archive extraction failed (reason=no_space_left)")

            def receipt_after_cleanup(path, report):
                self.assertFalse(destination.exists())
                _write_json(path, report)

            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream), \
                 patch.object(BackupManager, "_extract_recovery_stream", side_effect=exhausted), \
                 patch("app.backup._write_json", side_effect=receipt_after_cleanup):
                report = manager.restore_recovery_bundle(
                    Path(created["recovery_bundle_name"]), SECRET, restore_root=destination,
                )
            self.assertEqual(report["overall"], "FAIL")
            self.assertIn("no_space_left", report["error"])
            proof = json.loads((manager.backup_dir / (created["recovery_bundle_name"] + ".proof.json")).read_text())
            self.assertEqual(proof["restore"]["overall"], "FAIL")
            self.assertNotIn("offsite", proof)

    def test_restore_headroom_guard_stops_before_extracting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            archive_bytes = io.BytesIO()
            with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
                member = tarfile.TarInfo("private_storage/users/synthetic.bin")
                member.size = 16
                archive.addfile(member, io.BytesIO(b"x" * member.size))
            archive_bytes.seek(0)
            usage = shutil._ntuple_diskusage(10**9, 0, RESTORE_FREE_SPACE_MARGIN + 15)
            with patch("app.backup.shutil.disk_usage", return_value=usage):
                with self.assertRaisesRegex(BackupError, "reason=no_space_left"):
                    BackupManager._extract_recovery_stream(archive_bytes, destination)
            self.assertEqual(list(destination.iterdir()), [])

    def test_restored_sqlite_with_foreign_key_violation_fails_closed(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "synthetic.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE schema_migrations(version INTEGER);
                INSERT INTO schema_migrations VALUES(16);
                CREATE TABLE parent(id INTEGER PRIMARY KEY);
                CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));
                INSERT INTO child VALUES(123);
            """)
            connection.close()
            with self.assertRaisesRegex(BackupError, "foreign key"):
                BackupManager._validate_sqlite(database)

    def test_recovery_binds_commerce_fingerprints_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, _ = self.setup_manager(root)
            source = BackupManager._validate_sqlite(manager.database_path)
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                created = manager.create_recovery_bundle(SECRET)
                restored = manager.restore_recovery_bundle(
                    Path(created["recovery_bundle_name"]), SECRET, restore_root=root / "restored",
                )
            self.assertEqual(restored["overall"], "PASS")
            self.assertEqual(restored["foreign_key_check"], "ok")
            self.assertIn("payment_orders", restored["commerce_fingerprints"])
            self.assertEqual(restored["commerce_fingerprints"], source["commerce_fingerprints"])
            self.assertEqual(source, BackupManager._validate_sqlite(manager.database_path))

    def test_mismatched_commerce_manifest_fails_restore_without_success_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, _ = self.setup_manager(root)
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                created = manager.create_recovery_bundle(SECRET)
            original = manager.backup_dir / created["recovery_bundle_name"]
            altered = manager.backup_dir / "ravuna-recovery-20260101T000000000000Z.tar.gz.enc"
            with tarfile.open(original, "r:gz") as source, tarfile.open(altered, "w:gz") as target:
                for member in source:
                    contents = source.extractfile(member).read() if member.isfile() else None
                    if member.name == "manifest.json":
                        manifest = json.loads(contents)
                        manifest["sqlite"]["commerce_fingerprints"]["payment_orders"]["sha256"] = "0" * 64
                        contents = json.dumps(manifest).encode()
                        member.size = len(contents)
                    target.addfile(member, io.BytesIO(contents) if contents is not None else None)
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                result = manager.restore_recovery_bundle(Path(altered.name), SECRET, restore_root=root / "bad")
            self.assertEqual(result["overall"], "FAIL")
            self.assertIn("commerce fingerprint", result["error"])
            self.assertFalse((root / "bad").exists())

    def setup_manager(self, root):
        app = root / "app"
        (app / "data/users/synthetic").mkdir(parents=True)
        Database(app / "data/photo_bot.sqlite3")
        (app / ".deploy-sha").write_text("a" * 40)
        (app / ".env").write_text("SECRET=never_archive_this")
        source = app / "data/users/synthetic/input.bin"
        source.write_bytes(b"synthetic-private-file" * 100)
        return BackupManager(app / "data/photo_bot.sqlite3", app / "data/backups", 14), source

    def test_stream_roundtrip_has_no_media_copy_or_plain_tar_and_hashes_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, source = self.setup_manager(root)
            source_bytes = source.read_bytes()
            source_stat = source.stat()

            @contextmanager
            def inspected_stream(path, **kwargs):
                # Only a tiny DB snapshot and the private encrypted output may
                # exist in backup staging, never a users copy or plaintext tar.
                staged_names = sorted(p.name for p in path.parent.iterdir())
                self.assertEqual(staged_names, ["bundle.enc", "snapshot.sqlite3"])
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                with plain_stream(path, **kwargs) as output:
                    yield output

            with patch.object(BackupManager, "_openssl_stream", side_effect=inspected_stream), \
                 patch("app.backup.shutil.copytree", side_effect=AssertionError("no media staging")):
                created = manager.create_recovery_bundle(SECRET)
            bundle = manager.backup_dir / created["recovery_bundle_name"]
            with tarfile.open(bundle, "r:gz") as archive:
                names = archive.getnames()
                self.assertEqual(names[-1], "manifest.json")
                self.assertNotIn(".env", names)
                self.assertEqual(archive.extractfile("private_storage/users/synthetic/input.bin").read(), source_bytes)
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                restored = manager.restore_recovery_bundle(Path(bundle.name), SECRET, restore_root=root / "restore")
            self.assertEqual(restored["overall"], "PASS")
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(source.stat().st_mtime_ns, source_stat.st_mtime_ns)
            self.assertEqual(sorted(p.name for p in manager.backup_dir.iterdir()),
                             sorted([bundle.name, bundle.name + ".proof.json", ".recovery-retention.lock", "latest_recovery.json", "recovery_restore_status.json"]))

    def test_safe_openssl_reason_codes_do_not_expose_raw_stderr(self):
        for status, fragment, expected in (
            (1, "No space left on device", "no_space_left"),
            (1, "Permission denied", "permission_denied"),
            (1, "Input/output error", "io_error"),
            (-9, "", "openssl_killed"),
            (1, "bad decrypt", "invalid_passphrase_or_artifact"),
            (1, "unknown diagnostic", "openssl_failed"),
        ):
            with self.subTest(expected=expected):
                raw = f"{fragment}: /private/customer/name {SECRET}"
                result = subprocess.CompletedProcess([], status, stdout="", stderr=raw)
                with patch("app.backup.subprocess.run", return_value=result):
                    with self.assertRaises(BackupError) as raised:
                        BackupManager._openssl("-d", passphrase=SECRET)
                self.assertIn("reason=" + expected, str(raised.exception))
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertNotIn("/private", str(raised.exception))

    def test_failed_stream_removes_partial_artifact_plaintext_and_keeps_old_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _ = self.setup_manager(Path(directory))
            previous = manager.backup_dir / "latest_recovery.json"
            previous.write_text('{"unchanged":true}')

            @contextmanager
            def failing_stream(path, **kwargs):
                path.write_bytes(b"partial-encrypted-bytes")
                raise BackupError("Backup encryption operation failed (reason=no_space_left)")
                yield  # pragma: no cover

            with patch.object(BackupManager, "_openssl_stream", side_effect=failing_stream), \
                 patch.object(BackupManager, "prune") as prune:
                with self.assertRaisesRegex(BackupError, "no_space_left"):
                    manager.create_recovery_bundle(SECRET)
            prune.assert_not_called()
            self.assertEqual([p.name for p in manager.backup_dir.iterdir()], [previous.name])
            self.assertEqual(previous.read_text(), '{"unchanged":true}')

    def test_snapshot_enospc_is_safe_and_cleans_temporary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _ = self.setup_manager(Path(directory))
            with patch.object(BackupManager, "_sqlite_snapshot", side_effect=OSError(errno.ENOSPC, SECRET)):
                with self.assertRaisesRegex(BackupError, "no_space_left") as raised:
                    manager.create_recovery_bundle(SECRET)
            self.assertNotIn(SECRET, str(raised.exception))
            self.assertEqual(list(manager.backup_dir.iterdir()), [])

    def test_source_change_during_stream_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, source = self.setup_manager(Path(directory))
            original_read = _HashingReader.read

            def changed_read(reader, size=-1):
                result = original_read(reader, size)
                with source.open("ab") as stream:
                    stream.write(b"concurrent change")
                return result

            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream), \
                 patch.object(_HashingReader, "read", changed_read):
                with self.assertRaisesRegex(BackupError, "private_storage_changed"):
                    manager.create_recovery_bundle(SECRET)
            self.assertEqual(list(manager.backup_dir.iterdir()), [])

    def test_symlink_storage_is_rejected_without_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, source = self.setup_manager(root)
            try:
                (source.parent / "escape").symlink_to(root)
            except OSError:
                self.skipTest("symlink creation requires Windows privilege")
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                with self.assertRaisesRegex(BackupError, "symlink"):
                    manager.create_recovery_bundle(SECRET)
            self.assertEqual(list(manager.backup_dir.iterdir()), [])

    def test_failed_restore_removes_extracted_plaintext_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, _ = self.setup_manager(root)
            bundle = manager.backup_dir / "ravuna-recovery-20260101T000000000000Z.tar.gz.enc"
            with tarfile.open(bundle, "w:gz") as archive:
                safe = tarfile.TarInfo("private_storage/users/first.bin")
                safe.size = 6
                archive.addfile(safe, io.BytesIO(b"secret"))
                unsafe = tarfile.TarInfo("../escape")
                unsafe.size = 4
                archive.addfile(unsafe, io.BytesIO(b"test"))
            destination = root / "isolated"
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                result = manager.restore_recovery_bundle(Path(bundle.name), SECRET, restore_root=destination)
            self.assertEqual(result["overall"], "FAIL")
            self.assertFalse(destination.exists())
            self.assertFalse((root / "escape").exists())

    def test_gzip_crc_is_required_even_when_tar_members_are_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, _ = self.setup_manager(root)
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                created = manager.create_recovery_bundle(SECRET)
            bundle = manager.backup_dir / created["recovery_bundle_name"]
            contents = bytearray(bundle.read_bytes())
            contents[-8] ^= 1  # corrupt CRC only, not archived file contents
            bundle.write_bytes(contents)
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                result = manager.restore_recovery_bundle(Path(bundle.name), SECRET, restore_root=root / "crc")
            self.assertEqual(result["overall"], "FAIL")
            self.assertFalse((root / "crc").exists())

    def test_truncated_gzip_footer_is_a_safe_failed_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, _ = self.setup_manager(root)
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                created = manager.create_recovery_bundle(SECRET)
            bundle = manager.backup_dir / created["recovery_bundle_name"]
            bundle.write_bytes(bundle.read_bytes()[:-4])
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                result = manager.restore_recovery_bundle(Path(bundle.name), SECRET, restore_root=root / "truncated")
            self.assertEqual(result["overall"], "FAIL")
            self.assertEqual(result["error"], "Recovery archive integrity check failed")
            self.assertFalse((root / "truncated").exists())

    def test_real_openssl_roundtrip_wrong_password_safe_failure(self):
        if shutil.which("openssl") is None:
            self.skipTest("OpenSSL is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, _ = self.setup_manager(root)
            created = manager.create_recovery_bundle(SECRET)
            restored = manager.restore_recovery_bundle(Path(created["recovery_bundle_name"]), SECRET,
                                                       restore_root=root / "restore")
            self.assertEqual(restored["overall"], "PASS")
            failed = manager.restore_recovery_bundle(Path(created["recovery_bundle_name"]), SECRET + "wrong",
                                                     restore_root=root / "wrong")
            self.assertEqual(failed["overall"], "FAIL")
            # CBC occasionally has syntactically valid padding even with a bad
            # key. In that case gzip/tar integrity, not OpenSSL, rejects it.
            self.assertTrue("reason=invalid_passphrase_or_artifact" in failed["error"]
                            or failed["error"] == "Recovery archive integrity check failed")
            self.assertNotIn(SECRET, failed["error"])
            self.assertFalse((root / "wrong").exists())

    def test_negative_process_return_code_is_not_assumed_to_be_oom(self):
        self.assertEqual(_openssl_reason(-9, b""), "openssl_killed")
        self.assertNotIn("oom", _openssl_reason(-9, b""))

    def test_stream_subprocess_failure_preserves_safe_metadata_and_removes_output(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _ = self.setup_manager(Path(directory))

            class FailedProcess:
                def __init__(self, args, **kwargs):
                    self.stdin, self.stdout = io.BytesIO(), None
                    self.secret_fd = os.dup(kwargs["pass_fds"][0]) if "pass_fds" in kwargs else None
                    kwargs["stderr"].write(("write failed: No space left on device /private/file " + SECRET).encode())
                    Path(args[args.index("-out") + 1]).write_bytes(b"partial output")
                    if any(SECRET in value for value in args):
                        raise AssertionError("secret must not be in command arguments")

                def poll(self):
                    return 1

                def wait(self, **kwargs):
                    if self.secret_fd is not None:
                        os.close(self.secret_fd)
                        self.secret_fd = None
                    return 1

            with patch("app.backup.subprocess.Popen", side_effect=FailedProcess):
                with self.assertRaisesRegex(BackupError, "reason=no_space_left; exit_code=1") as raised:
                    manager.create_recovery_bundle(SECRET)
            self.assertNotIn(SECRET, str(raised.exception))
            self.assertNotIn("/private/file", str(raised.exception))
            self.assertEqual(list(manager.backup_dir.iterdir()), [])

    def test_legacy_manifest_first_archive_restores_through_real_stream(self):
        if shutil.which("openssl") is None:
            self.skipTest("OpenSSL is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, source = self.setup_manager(root)
            with patch.object(BackupManager, "_openssl_stream", side_effect=plain_stream):
                staged = manager.create_recovery_bundle(SECRET)
            staged_path = manager.backup_dir / staged["recovery_bundle_name"]
            legacy_plain = root / "synthetic-legacy.tar.gz"
            with tarfile.open(staged_path, "r:gz") as original, tarfile.open(legacy_plain, "w:gz") as archive:
                ordered = sorted(original.getmembers(), key=lambda member: member.name != "manifest.json")
                for member in ordered:
                    archive.addfile(member, original.extractfile(member) if member.isfile() else None)
            legacy = manager.backup_dir / "ravuna-recovery-20260101T000000000000Z.tar.gz.enc"
            manager._openssl("-salt", "-in", str(legacy_plain), "-out", str(legacy), passphrase=SECRET)
            report = manager.restore_recovery_bundle(Path(legacy.name), SECRET, restore_root=root / "legacy")
            self.assertEqual(report["overall"], "PASS")
            self.assertEqual((root / "legacy/private_storage/users/synthetic/input.bin").read_bytes(), source.read_bytes())
