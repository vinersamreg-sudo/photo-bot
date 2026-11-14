import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import Mock

from app.backup import BackupError, BackupManager
from app.ru_recovery import READBACK, _restore
from scripts.yandex_recovery_pilot import ApiError, alert_roundtrip, bucket_policy, cleanup, fixture


class RuPilotTests(TestCase):
    def test_policy_is_private_and_writer_permissions_are_prefix_scoped(self):
        rules = bucket_policy("ravuna-pilot-test", "full-writer", "db-writer", "reader")["Statement"]
        allowed = [r for r in rules if r["Effect"] == "Allow"]
        self.assertFalse(any(r["Principal"] == "*" for r in allowed))
        reader = [r for r in allowed if r["Principal"] == {"CanonicalUser": "reader"}]
        self.assertFalse(any(set(r["Action"]) & {"s3:PutObject", "s3:DeleteObject"} for r in reader))
        full = [r for r in allowed if r["Principal"] == {"CanonicalUser": "full-writer"}]
        self.assertNotIn("ravuna/db-v2", json.dumps(full))
        self.assertFalse(any("s3:DeleteBucketPolicy" in r["Action"] for r in allowed))

    def test_cleanup_refuses_unowned_bucket_and_foreign_keys(self):
        admin = Mock(bucket="ravuna-pilot-test")
        admin._api.return_value = {"TagSet": []}
        with self.assertRaises(BackupError):
            cleanup(admin, "nonce")
        self.assertEqual(admin._api.call_count, 1)
        admin._api.reset_mock()
        admin._api.side_effect = [{"TagSet": [{"Key": "RavunaRecoveryPilot", "Value":"nonce"}]},
                                  {"Contents":[{"Key":"unexpected"}]}, {}]
        with self.assertRaises(BackupError):
            cleanup(admin, "nonce")
        self.assertEqual([x.args[0] for x in admin._api.call_args_list],
                         ["get-bucket-tagging","list-objects-v2","list-multipart-uploads"])

    def test_cleanup_removes_only_then_verifies_owned_bucket(self):
        admin = Mock(bucket="ravuna-pilot-test")
        admin._api.side_effect = [{"TagSet": [{"Key":"RavunaRecoveryPilot","Value":"nonce"}]},
                                  {"Contents":[{"Key":"ravuna/full-v3/catalog.json"}]}, {}, {}, {}, {},
                                  ApiError("404")]
        cleanup(admin,"nonce")
        self.assertEqual([x.args[0] for x in admin._api.call_args_list][-4:],
                         ["delete-bucket-policy","delete-object","delete-bucket","head-bucket"])

    @skipUnless(shutil.which("openssl"), "OpenSSL required")
    def test_generated_pilot_fixture_restores_and_tls_receiver_acknowledges(self):
        from contextlib import contextmanager
        with TemporaryDirectory() as path:
            work=Path(path)
            with contextmanager(fixture)(work) as (source, full, db, keyfile):
                self.assertGreater(full.stat().st_size,64*1024**2)
                self.assertTrue(db.is_file())
                manager=BackupManager(source,work,14)
                shutil.copyfile(full,work/READBACK)
                result=_restore(manager,READBACK,keyfile.read_text(),work/"restored",
                                BackupManager._sha256(full))
                self.assertGreaterEqual(result["distinct_media"], 8)
                self.assertGreater(result["media_references"], result["distinct_media"])
            alert_roundtrip(work)
