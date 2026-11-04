import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.prune_backup_artifacts import PruneBlocked, execute, plan, resolve_previous, verify_readback


class BackupArtifactRetentionTests(unittest.TestCase):
    @staticmethod
    def artifact(number):
        return dict(id=number, name=f'ravuna-production-backup-{number}',
                    size_in_bytes=100, expired=False, digest='synthetic',
                    created_at=f'2026-11-0{number}T03:00:00Z', workflow_run={'id': number})

    @staticmethod
    def run(number):
        return {'id': number, 'path': '.github/workflows/backup.yml',
                'event': 'schedule', 'conclusion': 'success'}

    def test_keep_two_and_only_delete_older_successful_backup_workflows(self):
        artifacts = [self.artifact(i) for i in range(1, 5)]
        artifacts.append(dict(id=100, name='unrelated-CI', created_at='2020-01-01T00:00:00Z'))
        result = plan(artifacts, [3, 4], self.run)
        self.assertEqual([e['id'] for e in result if e['action'] == 'DELETE'], [1, 2])
        self.assertEqual([e['id'] for e in result if e['action'] == 'KEEP'], [3, 4])

    def test_full_ciphertext_readback_is_read_only_and_rejects_bad_hash_and_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = 'ravuna-recovery-20261104T091302549292Z.tar.gz.enc'
            data = b'synthetic ciphertext'
            file = root / name
            file.write_bytes(data)
            sha = hashlib.sha256(data).hexdigest()
            before = file.read_bytes(), file.stat().st_mtime_ns
            report = verify_readback(root, name, sha)
            self.assertEqual(report['overall'], 'PASS')
            self.assertEqual(report['bytes'], len(data))
            self.assertEqual(before, (file.read_bytes(), file.stat().st_mtime_ns))
            for bad_name, bad_sha in ((name, '0' * 64), ('../' + name, sha), (name, 'invalid')):
                with self.subTest(name=bad_name), self.assertRaises(PruneBlocked):
                    verify_readback(root, bad_name, bad_sha)

    def test_readback_workflow_cannot_enter_production_backup_job(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/backup.yml').read_text()
        readback = workflow.split('  readback:', 1)[1].split('  backup:', 1)[0]
        self.assertIn('actions: read', readback)
        self.assertIn('verify_readback', readback)
        for forbidden in ('secrets.', 'environment: production', 'ssh ', '--apply', 'recovery-create'):
            self.assertNotIn(forbidden, readback)
        self.assertIn("if: github.event_name != 'workflow_dispatch' || !inputs.readback_only", workflow)

    def test_missing_expired_and_non_distinct_protected_points_block(self):
        original = [self.artifact(i) for i in range(1, 5)]
        for ids in ([4], [4, 4], [4, 5]):
            with self.subTest(ids=ids), self.assertRaises(PruneBlocked):
                plan(original, ids, self.run)
        original[-1]['expired'] = True
        with self.assertRaises(PruneBlocked):
            plan(original, [3, 4], self.run)

    def test_failed_unknown_newer_and_held_artifacts_are_preserved(self):
        artifacts = [self.artifact(i) for i in range(1, 6)]
        def runs(number):
            result = self.run(number)
            if number == 2:
                result['conclusion'] = 'failure'
            return result
        result = plan(artifacts, [3, 4], runs, held_ids=[1])
        actions = {e['id']: e['action'] for e in result}
        self.assertEqual(actions[1], 'PRESERVE_HOLD')
        self.assertEqual(actions[2], 'PRESERVE_UNKNOWN')
        self.assertEqual(actions[5], 'PRESERVE_UNKNOWN')
        self.assertNotIn('DELETE', actions.values())

    def test_dry_run_never_deletes_and_apply_rechecks_every_identity_first(self):
        artifacts = [self.artifact(i) for i in range(1, 5)]
        deleted = []
        def fake_api(repository, path, method='GET'):
            if path.startswith('actions/artifacts?'):
                return {'artifacts': copy.deepcopy(artifacts)}
            if path.startswith('actions/runs/'):
                return self.run(int(path.rsplit('/', 1)[1]))
            number = int(path.rsplit('/', 1)[1])
            if method == 'DELETE':
                deleted.append(number)
                return None
            return copy.deepcopy(next(a for a in artifacts if a['id'] == number))
        with patch('scripts.prune_backup_artifacts.api', side_effect=fake_api):
            self.assertEqual(execute('synthetic/repo', [3, 4])['removed'], 0)
            self.assertEqual(deleted, [])
            self.assertEqual(execute('synthetic/repo', [3, 4], apply=True)['removed'], 2)
            self.assertEqual(deleted, [1, 2])
        def changed(repository, path, method='GET'):
            result = fake_api(repository, path, method)
            if path == 'actions/artifacts/4': result['digest'] = 'changed'
            return result
        deleted.clear()
        with patch('scripts.prune_backup_artifacts.api', side_effect=changed):
            with self.assertRaises(PruneBlocked): execute('synthetic/repo', [3, 4], apply=True)
        self.assertEqual(deleted, [])

    def test_previous_receipt_mapping_and_unknown_provider_fail_closed(self):
        with patch('scripts.prune_backup_artifacts.api', return_value=self.artifact(4)):
            self.assertEqual(resolve_previous('synthetic/repo', 'github-actions:4:4')['artifact_id'], 4)
        with self.assertRaises(PruneBlocked): resolve_previous('synthetic/repo', 'unknown')

    def test_legacy_mapping_uses_exact_creation_window_not_latest_success(self):
        runs = [dict(self.run(3), created_at='2026-10-26T06:00:00Z',
                     updated_at='2026-10-26T07:00:00Z'),
                dict(self.run(4), created_at='2026-11-04T09:00:00Z',
                     updated_at='2026-11-04T10:00:00Z')]
        def fake_api(repository, path, method='GET'):
            if 'workflows/backup.yml/runs?' in path:
                return {'workflow_runs': runs}
            self.assertEqual(path, 'actions/runs/3/artifacts')
            return {'artifacts': [self.artifact(3)]}
        with patch('scripts.prune_backup_artifacts.api', side_effect=fake_api):
            self.assertEqual(resolve_previous('synthetic/repo', 'github-actions',
                'ravuna-recovery-20261026T061721629733Z.tar.gz.enc')['run_id'], 3)
        runs.append(dict(runs[0], id=2))
        with patch('scripts.prune_backup_artifacts.api', side_effect=fake_api):
            with self.assertRaises(PruneBlocked):
                resolve_previous('synthetic/repo', 'github-actions',
                    'ravuna-recovery-20261026T061721629733Z.tar.gz.enc')


if __name__ == '__main__': unittest.main()
