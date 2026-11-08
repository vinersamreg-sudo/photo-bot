import io
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.backup import BackupError, BackupManager
from app.database import Database
from scripts.backup_tool import main, prepare_recovery, previous_point
from tests.test_backup_streaming import SECRET, plain_stream


class BackupToolTests(TestCase):
    def manager(self, root):
        (root / 'data/users').mkdir(parents=True)
        Database(root / 'data/photo_bot.sqlite3')
        (root / '.deploy-sha').write_text('a' * 40)
        (root / '.env').write_text('IMAGE_PROVIDER=must-not-be-initialized\nBACKUP_RETENTION_DAYS=14\n')
        return BackupManager(root / 'data/photo_bot.sqlite3', root / 'data/backups', 14)

    def test_cli_uses_existing_db_without_application_or_provider_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self.manager(root)
            before = manager.database_path.read_bytes(), (root / '.env').read_bytes()
            with patch('sys.stdin', io.StringIO(SECRET + '\n')), patch('builtins.print') as output, \
                 patch.object(BackupManager, '_openssl_stream', side_effect=plain_stream):
                self.assertEqual(main(['--root', str(root), 'recovery-create', '--passphrase-stdin']), 0)
            self.assertIn('recovery_bundle_name', json.loads(output.call_args.args[0]))
            self.assertEqual(before, (manager.database_path.read_bytes(), (root / '.env').read_bytes()))

    def test_cli_does_not_initialize_missing_db_or_backup_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('builtins.print'):
                self.assertEqual(main(['--root', str(root), 'recovery-create', '--passphrase-stdin']), 1)
            self.assertFalse((root / 'data').exists())

    def test_retry_reuses_pending_point_and_failed_restore_requires_review(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(Path(directory))
            with patch.object(BackupManager, '_openssl_stream', side_effect=plain_stream):
                created = manager.create_recovery_bundle(SECRET)
            with patch.object(BackupManager, 'create_recovery_bundle') as create:
                self.assertEqual(prepare_recovery(manager, SECRET), created)
                create.assert_not_called()
            receipt = manager.backup_dir / (created['recovery_bundle_name'] + '.proof.json')
            proof = json.loads(receipt.read_text())
            proof['restore'] = {'overall': 'FAIL'}
            receipt.write_text(json.dumps(proof))
            with patch.object(BackupManager, 'create_recovery_bundle') as create:
                with self.assertRaisesRegex(BackupError, 'operator review'):
                    prepare_recovery(manager, SECRET)
                create.assert_not_called()

    def test_capacity_gate_fails_before_allocation_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(Path(directory))
            before = manager.database_path.read_bytes()
            with patch('app.backup.shutil.disk_usage', return_value=SimpleNamespace(free=1)), \
                 patch.object(BackupManager, '_sqlite_snapshot') as snapshot:
                with self.assertRaisesRegex(BackupError, 'no_space_left'):
                    manager.create_recovery_bundle(SECRET)
                snapshot.assert_not_called()
            self.assertEqual(list(manager.backup_dir.iterdir()), [])
            self.assertEqual(before, manager.database_path.read_bytes())

    def test_scheduled_rotation_warns_about_legacy_only_with_two_verified_survivors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.manager(root)
            report = {"successful": 3, "removed": 1, "entries": [{"action": "PRESERVE_UNKNOWN"}]}
            args = ["--root", str(root), "recovery-prune", "--confirmed-backup", "synthetic", "--apply"]
            with patch.object(BackupManager, "prune_recovery_by_count", return_value=report), +                 patch("builtins.print") as output:
                self.assertEqual(main(args), 1)
                self.assertEqual(main(args + ["--report-preserved-legacy"]), 0)
                self.assertIn("Legacy unverified recovery preserved", output.call_args.args[0])
                report["removed"] = 0
                self.assertEqual(main(args + ["--report-preserved-legacy"]), 1)

    def test_daily_repeat_reuses_verified_point_and_previous_is_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(Path(directory))
            points = []
            with patch.object(BackupManager, '_openssl_stream', side_effect=plain_stream):
                for number in range(2):
                    created = manager.create_recovery_bundle(SECRET)
                    manager.restore_recovery_bundle(Path(created['recovery_bundle_name']), SECRET)
                    manager.mark_recovery_offsite(created['recovery_bundle_name'], 'synthetic')
                    points.append(created)
            with patch.object(BackupManager, 'create_recovery_bundle') as create:
                self.assertEqual(prepare_recovery(manager, SECRET), points[-1])
                create.assert_not_called()
            self.assertEqual(previous_point(manager)['backup_name'], points[0]['recovery_bundle_name'])
            self.assertEqual(len(list(manager.backup_dir.glob('ravuna-recovery-*.tar.gz.enc'))), 2)

    def test_gallery_reference_checks_original_preview_and_skips_purged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self.manager(root)
            database = root / 'minimal.sqlite3'
            c = sqlite3.connect(database)
            c.executescript('CREATE TABLE gallery_items(id INTEGER,purged_at TEXT); '
                            'CREATE TABLE gallery_versions(gallery_item_id INTEGER,original_path TEXT,preview_watermarked_path TEXT); '
                            'INSERT INTO gallery_items VALUES(1,NULL),(2,"purged");')
            original = manager.users_dir / 'synthetic/original.jpg'
            preview = manager.users_dir / 'synthetic/preview.jpg'
            c.execute('INSERT INTO gallery_versions VALUES(1,?,?)', (str(original), str(preview)))
            c.execute('INSERT INTO gallery_versions VALUES(2,"/outside/purged",NULL)')
            c.commit(); c.close()
            restored_users = root / 'restored/users'
            for name in ('original.jpg', 'preview.jpg'):
                file = restored_users / 'synthetic' / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b'synthetic')
            self.assertEqual(manager._validate_gallery_files(database, restored_users), {'originals': 1, 'previews': 1})
            (restored_users / 'synthetic/preview.jpg').unlink()
            with self.assertRaisesRegex(BackupError, 'missing or unsafe'):
                manager._validate_gallery_files(database, restored_users)
