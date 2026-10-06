import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.backup import BackupError, BackupManager
from app.recovery_retention import RetentionError, artifact, main, prune, read_json, write_stage
from tests.test_backup_streaming import SECRET, plain_stream


class RecoveryRetentionTests(TestCase):
    def snapshot(self, root, number, stages=3):
        created = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=number)
        name = 'ravuna-recovery-' + created.strftime('%Y%m%dT%H%M%S%fZ') + '.tar.gz.enc'
        path = root / name
        path.write_bytes(('synthetic-' + str(number)).encode())
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        write_stage(root, name, 'creation', {'created_at': created.isoformat(),
                                            'sha256': sha, 'size_bytes': path.stat().st_size})
        if stages >= 2:
            write_stage(root, name, 'restore', {'overall': 'PASS', 'artifact_status': 'PASS',
                        'sqlite_status': 'PASS', 'restore_status': 'PASS', 'sha256': sha,
                        'tested_at': (created + timedelta(minutes=1)).isoformat()})
        if stages >= 3:
            write_stage(root, name, 'offsite', {'overall': 'PASS', 'sha256': sha,
                        'provider': 'synthetic-test',
                        'copied_at': (created + timedelta(minutes=2)).isoformat()})
        return path

    def test_thirteen_successes_keep_latest_seven_not_seven_days(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [self.snapshot(root, i) for i in range(13)]
            expected = sum(p.stat().st_size for p in files[:6])
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            dry = prune(root, confirmed_backup=files[-1].name)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            self.assertEqual(dry['delete_bytes'], expected)
            self.assertEqual([e['backup_name'] for e in dry['entries'] if e['action'] == 'KEEP'], [p.name for p in files[6:]])
            self.assertEqual(dry, prune(root, confirmed_backup=files[-1].name))
            applied = prune(root, confirmed_backup=files[-1].name, dry_run=False)
            self.assertEqual(applied['removed'], 6)
            self.assertTrue(all(p.exists() for p in files[6:]))
            self.assertFalse(any(p.exists() for p in files[:6]))
            self.assertEqual(prune(root, confirmed_backup=files[-1].name, dry_run=False)['removed'], 0)

    def test_each_missing_gate_and_invalid_keep_blocks_all_deletion(self):
        for stages in (1, 2):
            with self.subTest(stages=stages), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                older = self.snapshot(root, 1)
                newest = self.snapshot(root, 2, stages=stages)
                with self.assertRaises(RetentionError):
                    prune(root, confirmed_backup=newest.name, keep=1, dry_run=False)
                with self.assertRaises(RetentionError):
                    prune(root, confirmed_backup=older.name, keep=1, dry_run=False)
                self.assertTrue(older.exists())
                self.assertTrue(newest.exists())
        with tempfile.TemporaryDirectory() as directory:
            newest = self.snapshot(Path(directory), 1)
            for value in (0, -1, True):
                with self.assertRaises(RetentionError):
                    prune(Path(directory), confirmed_backup=newest.name, keep=value)

    def test_unknown_partial_failed_hash_malformed_proofs_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [self.snapshot(root, i) for i in range(8)]
            (root / (files[0].name + '.proof.json')).unlink()
            (root / (files[1].name + '.proof.json')).write_text('{broken')
            files[2].write_bytes(b'tampered')
            proof = read_json(root / (files[3].name + '.proof.json'))
            proof['restore']['overall'] = 'FAIL'
            (root / (files[3].name + '.proof.json')).write_text(json.dumps(proof))
            proof = read_json(root / (files[4].name + '.proof.json'))
            proof['offsite']['sha256'] = 'f' * 64
            (root / (files[4].name + '.proof.json')).write_text(json.dumps(proof))
            partial = root / 'ravuna-recovery-partial.tar.gz.enc'
            partial.write_bytes(b'partial')
            dry = prune(root, confirmed_backup=files[-1].name, keep=1)
            self.assertEqual(sum(e['action'] == 'PRESERVE_UNKNOWN' for e in dry['entries']), 6)
            prune(root, confirmed_backup=files[-1].name, keep=1, dry_run=False)
            self.assertTrue(all(p.exists() for p in files[:5]))
            self.assertTrue(partial.exists())
            self.assertTrue(files[-1].exists())

    def test_links_and_symlink_receipts_never_count_or_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = self.snapshot(root, 1)
            newest = self.snapshot(root, 3)
            alias = root / 'ravuna-recovery-20260101T020000000000Z.tar.gz.enc'
            try:
                alias.symlink_to(older)
            except OSError:
                self.skipTest('Symlink creation unavailable')
            receipt = root / (older.name + '.proof.json')
            saved = root / 'saved.json'
            receipt.rename(saved)
            receipt.symlink_to(saved)
            report = prune(root, confirmed_backup=newest.name, keep=1, dry_run=False)
            self.assertEqual(report['removed'], 0)
            self.assertTrue(alias.is_symlink())
            self.assertTrue(older.exists())

    def test_race_after_hashing_blocks_before_first_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [self.snapshot(root, i) for i in range(3)]
            from app.recovery_retention import plan as original
            def changed(*args, **kwargs):
                result = original(*args, **kwargs)
                files[0].write_bytes(b'replaced-artifact')
                return result
            with patch('app.recovery_retention.plan', side_effect=changed):
                with self.assertRaises(RetentionError):
                    prune(root, confirmed_backup=files[-1].name, keep=1, dry_run=False)
            self.assertTrue(all(p.exists() for p in files))

    def test_sqlite_age_retention_never_touches_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = BackupManager(root / 'data/photo_bot.sqlite3', root / 'backups', 14)
            old = manager.backup_dir / 'pixora-old.sqlite3.enc'
            new = manager.backup_dir / 'pixora-new.sqlite3.enc'
            recovery = self.snapshot(manager.backup_dir, 1)
            old.write_bytes(b'old')
            new.write_bytes(b'new')
            now = datetime.now(timezone.utc)
            for path in (old, recovery):
                os.utime(path, ((now - timedelta(days=20)).timestamp(),) * 2)
            self.assertEqual(manager.prune(now), 1)
            self.assertTrue(new.exists())
            self.assertTrue(recovery.exists())

    def test_proof_race_blocks_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [self.snapshot(root, i) for i in range(3)]
            from app.recovery_retention import plan as original
            def changed(*args, **kwargs):
                result = original(*args, **kwargs)
                # Simulate a non-cooperating writer; cooperating stage writes
                # are already serialized by the same retention lock.
                receipt = root / (files[-1].name + '.proof.json')
                value = read_json(receipt)
                value['restore']['overall'] = 'FAIL'
                receipt.write_text(json.dumps(value))
                return result
            with patch('app.recovery_retention.plan', side_effect=changed):
                with self.assertRaises(RetentionError):
                    prune(root, confirmed_backup=files[-1].name, keep=1, dry_run=False)
            self.assertTrue(all(p.exists() for p in files))

    def test_real_lifecycle_receipts_do_not_prune_before_offsite(self):
        with tempfile.TemporaryDirectory() as directory:
            from tests.test_backup_streaming import BackupStreamingTests
            manager, _ = BackupStreamingTests().setup_manager(Path(directory))
            with patch.object(BackupManager, '_openssl_stream', side_effect=plain_stream), \
                 patch.object(BackupManager, 'prune_recovery_by_count') as prune_mock:
                created = manager.create_recovery_bundle(SECRET)
                name = created['recovery_bundle_name']
                with self.assertRaises(BackupError):
                    manager.mark_recovery_offsite(name, 'synthetic')
                report = manager.restore_recovery_bundle(Path(name), SECRET)
                self.assertEqual(report['overall'], 'PASS')
                with self.assertRaises(RetentionError):
                    prune(manager.backup_dir, confirmed_backup=name)
                manager.mark_recovery_offsite(name, 'synthetic')
                prune_mock.assert_not_called()
            result = manager.prune_recovery_by_count(confirmed_backup=name)
            self.assertEqual(result['successful'], 1)
            self.assertEqual(result['removed'], 0)

    def test_legacy_external_evidence_is_hash_bound_and_dry_run_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = self.snapshot(root, 1)
            newest = self.snapshot(root, 2)
            receipt = root / (old.name + '.proof.json')
            external = {old.name: read_json(receipt)}
            receipt.unlink()
            report = prune(root, confirmed_backup=newest.name, keep=1, evidence=external)
            self.assertEqual(report['delete_bytes'], old.stat().st_size)
            old.write_bytes(b'changed')
            report = prune(root, confirmed_backup=newest.name, keep=1, evidence=external)
            self.assertEqual(report['delete_bytes'], 0)

    def test_cli_default_is_read_only_and_workflow_prunes_after_all_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = self.snapshot(root, 1)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with patch('app.config.load_settings') as settings, patch('builtins.print'):
                settings.return_value.backup_dir_path = root
                self.assertEqual(main(['--confirmed-backup', file.name]), 0)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/backup.yml').read_text()
        prune_at = workflow.index('recovery-prune --keep 7')
        for gate in ('recovery-create', 'test "$RESTORE_OVERALL" = PASS',
                     'actions/upload-artifact@v4', 'recovery-mark-offsite'):
            self.assertLess(workflow.index(gate), prune_at)
        self.assertLess(workflow.index('Deploy count-retention implementation'), workflow.index('REPORT=$(printf'))
