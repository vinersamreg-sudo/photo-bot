import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import threading
import io
from contextlib import redirect_stdout, redirect_stderr

from PIL import Image
from app.config import Settings
from app.database import Database
from app.storage import PrivateStorage
from app.gallery import GalleryService
from app.commerce import CommerceService
from app.media_guard import media_guard
from app.result_retention import ResultRetention, visible_results, _safe_metadata, _unlink
from app.work_gallery import WorkGallery
from app.demo_service import DemoService
from app.image_provider import FakeImageProvider
from app.watermark import WatermarkService
from app.domain import InvalidInputError, DeliveryError
from app.referrals import ReferralService
from app.max_conversation import MaxConversationStore


NOW = datetime(2026,10,16,10,tzinfo=timezone.utc)


def fixture_unlink(path,root,identity):
    # Windows unit fixtures cannot exercise POSIX dir_fd. Linux CI uses the real
    # implementation; no Windows production deletion fallback is introduced.
    if os.name=='posix':return _unlink(path,root,identity)
    assert _safe_metadata(path,root)==identity
    path.unlink()


class ResultRetentionTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.settings=Settings('', 'fake', 'test', self.base)
        self.db=Database(self.settings.database_path)
        self.storage=PrivateStorage(self.settings.users_dir,1024*1024)
        self.gallery=GalleryService(self.db,self.storage,self.settings,lambda:NOW)
        self.commerce=CommerceService(self.db,lambda:NOW)
        self.retention=ResultRetention(self.settings,self.db,lambda:NOW)
        self.source=self.base/'synthetic.png';Image.new('RGB',(4,4),'blue').save(self.source)
        with self.db.transaction() as c:
            for user in ('u','other'):
                c.execute("INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,'max',?,?)",(user,user,NOW.isoformat()))
                self.commerce.ensure_initial_grant(c,user)

    def seed(self,n,*,same_item=False,user='u',start=0):
        result=[];item=None
        for index in range(start,start+n):
            if item is None or not same_item:
                item=self.gallery.create_item(user,'Моя работа',self.source)
            v=f'{user}-v-{index:03}'
            root=self.storage.gallery_item_root(user,item.id)/'versions'/v
            root.mkdir(parents=True)
            paths=[]
            for name in ('source.png','original.png','preview.png'):
                path=root/name;path.write_bytes(self.source.read_bytes());paths.append(path)
            with self.db.transaction() as c:
                c.execute("""INSERT OR IGNORE INTO demo_sessions(id,user_id,source_file_path,source_sha256,status,
                    started_at,expires_at,max_generations,created_at,updated_at) VALUES(?,?,'','synthetic','active',?,?,100,?,?)""",
                    ('s-'+user,user,NOW.isoformat(),(NOW+timedelta(days=30)).isoformat(),NOW.isoformat(),NOW.isoformat()))
                c.execute("""INSERT INTO generation_attempts(id,idempotency_key,session_id,user_id,prompt,status,
                    started_at,completed_at,provider,model,source_path,original_result_path,demo_result_path,created_at)
                    VALUES(?,?,?,?, 'synthetic','succeeded',?,?,'fake','fake',?,?,?,?)""",
                    ('a-'+v,'key-'+v,'s-'+user,user,NOW.isoformat(),(NOW+timedelta(seconds=index)).isoformat(),str(paths[0]),str(paths[1]),str(paths[2]),NOW.isoformat()))
                c.execute("""INSERT INTO gallery_versions(id,gallery_item_id,version_number,parent_version_id,
                    source_path,prompt,effective_prompt,provider,model,preview_watermarked_path,original_path,
                    created_at,status) VALUES(?,?,?,?,?,'synthetic','synthetic','fake','fake',?,?,?,'succeeded')""",
                    (v,item.id,index+1,result[-1][0] if same_item and result else None,str(paths[0]),str(paths[2]),str(paths[1]),(NOW+timedelta(seconds=index)).isoformat()))
                c.execute('UPDATE gallery_versions SET attempt_id=? WHERE id=?',('a-'+v,v))
                c.execute('UPDATE gallery_items SET current_best_version_id=?,cover_preview_path=? WHERE id=?',(v,str(paths[2]),item.id))
            result.append((v,item.id,paths))
        return result

    def apply(self):
        with patch('app.result_retention._unlink',side_effect=fixture_unlink):
            return self.retention.run(execute=True)

    def financial(self):
        with self.db.read() as c:
            return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY 1')]
                    for t in ('users','user_credit_accounts','generation_credit_lots','credit_ledger',
                              'unlock_entitlements','payment_orders','continuation_pack_grants','referral_bonus_grants','bonus_credit_transactions')}

    def age_results(self):
        with self.db.transaction() as c:
            c.execute('UPDATE gallery_items SET created_at=?,retention_until=?',
                      ((NOW-timedelta(days=40)).isoformat(),(NOW-timedelta(days=10)).isoformat()))
            c.execute('UPDATE generation_attempts SET completed_at=?',((NOW-timedelta(days=31)).isoformat(),))
            c.execute('UPDATE gallery_versions SET created_at=?',((NOW-timedelta(days=31)).isoformat(),))

    def legacy_review(self, seeded):
        with self.db.read() as c:
            from app.result_retention import successful_results
            rows={r['id']:r for r in successful_results(c)}
        value={'schema_version':1,'basis':'legacy_expiry_30d','reviewed_at':NOW.isoformat(),
               'results':[{'version_id':v,'user_id':rows[v]['user_id'],
                           'gallery_item_id':rows[v]['gallery_item_id'],
                           'work_created_at':rows[v]['work_created_at'],
                           'completed_at':rows[v]['completed_at'],
                           'terms_sha256':'a'*64,'evidence_sha256':'b'*64} for v,*_ in seeded]}
        path=self.base/'private-review.json'
        path.write_text(json.dumps(value),encoding='utf-8');path.chmod(0o600)
        return path

    def first_apply(self, path):
        with patch('app.result_retention._unlink',side_effect=fixture_unlink):
            return self.retention.run(execute=True,first_batch=True,review_path=path)

    def test_first_batch_unknown_terms_means_zero_and_readonly(self):
        self.seed(15);self.age_results()
        before=self.settings.database_path.read_bytes()
        result=self.retention.run(first_batch=True)
        self.assertEqual(result['candidate_results'],0)
        self.assertEqual(result['legacy_expired_results'],15)
        self.assertEqual(result['protection_reasons']['legacy_terms_unverified'],15)
        self.assertEqual(self.settings.database_path.read_bytes(),before)
        self.assertFalse((self.settings.data_dir/'result_retention_last.json').exists())
        with self.assertRaises(ValueError):self.first_apply(None)

    def test_first_batch_ten_targets_preview_apply_and_replay_do_not_spill(self):
        seeded=self.seed(25);self.age_results();path=self.legacy_review(seeded[:10])
        cache=self.settings.temp_dir/'max-contact-sheets';cache.mkdir(parents=True)
        cached=cache/'synthetic.jpg';cached.write_bytes(b'cache');os.utime(cached,(0,0))
        before=self.financial()
        preview=self.retention.run(first_batch=True,review_path=path)
        self.assertEqual(preview['candidate_results'],10)
        self.assertEqual(preview['batch_limit'],10)
        done=self.first_apply(path)
        self.assertEqual(done['removed_versions'],10)
        self.assertEqual(done['review_sha256'],preview['review_sha256'])
        self.assertEqual(before,self.financial());self.assertTrue(cached.exists())
        self.assertEqual(self.first_apply(path)['removed_files'],0)
        for _,_,paths in seeded[10:]:self.assertTrue(all(p.exists() for p in paths))

    def test_first_batch_review_cannot_allow_early_eviction_or_fresh_correction(self):
        seeded=self.seed(6);path=self.legacy_review(seeded[:3])
        report=self.retention.run(first_batch=True,review_path=path)
        self.assertEqual(report['candidate_results'],0)
        self.assertEqual(report['protection_reasons']['early_eviction_excluded'],3)
        self.age_results()
        with self.db.transaction() as c:
            c.execute('UPDATE generation_attempts SET completed_at=? WHERE id=?',
                      (NOW.isoformat(),'a-'+seeded[0][0]))
        path=self.legacy_review(seeded[:1])
        self.assertEqual(self.first_apply(path)['removed_files'],0)

    def test_first_batch_invalid_or_changed_evidence_fails_closed(self):
        seeded=self.seed(15);self.age_results()
        path=self.legacy_review(seeded[:11])
        with self.assertRaises(ValueError):self.first_apply(path)
        path=self.legacy_review(seeded[:1])
        with self.db.transaction() as c:
            c.execute('UPDATE generation_attempts SET completed_at=? WHERE id=?',
                      ((NOW-timedelta(days=32)).isoformat(),'a-'+seeded[0][0]))
        result=self.first_apply(path)
        self.assertEqual(result['removed_files'],0)
        self.assertEqual(result['protection_reasons']['legacy_review_target_mismatch'],1)
        value=json.loads(path.read_text());value['results'][0]['evidence_sha256']=''
        path.write_text(json.dumps(value));path.chmod(0o600)
        with self.assertRaises(ValueError):self.first_apply(path)

    def test_first_batch_replans_after_preview_and_preserves_pending_payment(self):
        seeded=self.seed(5);self.age_results();path=self.legacy_review(seeded[:1])
        self.assertEqual(self.retention.run(first_batch=True,review_path=path)['candidate_results'],1)
        with self.db.transaction() as c:
            c.execute("""INSERT INTO payment_orders(id,public_token,attempt_id,version_id,user_id,
                provider,merchant_hash,provider_invoice_id,amount_minor,currency,status,description,
                created_at,updated_at,expires_at) VALUES('late','opaque','legacy',?,'u','robokassa',
                'synthetic',1,4900,'RUB','expired','synthetic',?,?,?)""",
                (seeded[0][0],NOW.isoformat(),NOW.isoformat(),NOW.isoformat()))
        before=self.financial();result=self.first_apply(path)
        self.assertEqual(result['removed_files'],0);self.assertEqual(before,self.financial())
        self.assertEqual(result['protection_reasons']['payment_or_delivery_obligation'],1)

    def test_first_batch_old_fully_spent_customer_is_not_free(self):
        seeded=self.seed(5);self.age_results()
        with self.db.transaction() as c:
            c.execute('UPDATE generation_attempts SET completed_at=?',((NOW-timedelta(days=400)).isoformat(),))
            c.execute("""INSERT INTO payment_orders(id,public_token,attempt_id,version_id,user_id,
                provider,merchant_hash,provider_invoice_id,amount_minor,currency,status,description,
                created_at,updated_at,expires_at,paid_at) VALUES('old','opaque','legacy',?,'u','robokassa',
                'synthetic',1,4900,'RUB','delivered','synthetic',?,?,?,?)""",
                (seeded[0][0],*((NOW-timedelta(days=400)).isoformat(),)*4))
        path=self.legacy_review(seeded[:1])
        result=self.first_apply(path)
        self.assertEqual(result['removed_files'],0)
        self.assertEqual(result['protection_reasons']['commerce_history_first_batch'],5)

    def test_first_batch_trash_editing_and_automatic_flag_are_not_bypassed(self):
        seeded=self.seed(6);self.age_results();path=self.legacy_review(seeded[:2])
        self.gallery.soft_delete('u',seeded[0][1])
        store=MaxConversationStore(self.db,lambda:NOW)
        store.get_or_create('u',None);store.transition('u','waiting_for_correction',force=True,user_id='u',
                                      current_gallery_item_id=seeded[1][1],current_version_id=seeded[1][0])
        result=self.first_apply(path)
        self.assertEqual(result['removed_files'],0)
        self.assertEqual(result['protection_reasons']['trash_retention'],1)
        self.assertEqual(result['protection_reasons']['editing_dialog'],1)
        active=ResultRetention(replace(self.settings,result_retention_enabled=True),self.db,lambda:NOW)
        with self.assertRaises(ValueError):active.run(execute=True,first_batch=True,review_path=path)

    def test_first_batch_failure_retry_stays_in_reviewed_set(self):
        seeded=self.seed(13);self.age_results();path=self.legacy_review(seeded[:2]);calls=[]
        def fail_after_first(path,root,identity):
            calls.append(path)
            if len(calls)>1:raise OSError('synthetic interruption')
            fixture_unlink(path,root,identity)
        before=self.financial()
        with patch('app.result_retention._unlink',side_effect=fail_after_first):
            with self.assertRaises(OSError):self.retention.run(execute=True,first_batch=True,review_path=path)
        self.first_apply(path)
        self.assertEqual(before,self.financial())
        for _,_,paths in seeded[2:]:self.assertTrue(all(p.exists() for p in paths))

    def test_first_batch_wrong_owner_and_changed_review_are_rejected(self):
        seeded=self.seed(5);self.age_results();path=self.legacy_review(seeded[:1])
        value=json.loads(path.read_text());value['results'][0]['user_id']='other'
        path.write_text(json.dumps(value));path.chmod(0o600)
        result=self.first_apply(path)
        self.assertEqual(result['removed_files'],0)
        self.assertEqual(result['protection_reasons']['legacy_review_target_mismatch'],1)
        path=self.legacy_review(seeded[:1])
        from app.result_retention import _legacy_review
        calls=[]
        def swapped(review,now):
            calls.append(1)
            if len(calls)==2:self.legacy_review(seeded[1:2])
            return _legacy_review(review,now)
        with patch('app.result_retention._legacy_review',side_effect=swapped):
            with self.assertRaises(RuntimeError):self.first_apply(path)
        for _,_,paths in seeded:self.assertTrue(all(p.exists() for p in paths))

    def test_first_batch_cli_rejects_unbounded_execution_before_db_access(self):
        from app.result_retention import main
        with patch.object(sys,'argv',['results-cleanup','--execute']), redirect_stderr(io.StringIO()):
            with patch('app.result_retention.Database') as database:
                with self.assertRaises(SystemExit) as stopped:main()
                self.assertEqual(stopped.exception.code,2);database.assert_not_called()
        with patch.object(sys,'argv',['results-cleanup','--first-batch']), redirect_stdout(io.StringIO()) as output:
            with patch('app.config.load_settings',return_value=self.settings):self.assertEqual(main(),0)
        self.assertEqual(json.loads(output.getvalue())['candidate_results'],0)

    @skipUnless(os.name=='posix','Production private evidence permissions')
    def test_first_batch_public_or_symlink_review_refused_without_unlink(self):
        seeded=self.seed(4);self.age_results();path=self.legacy_review(seeded[:1])
        path.chmod(0o644)
        with self.assertRaises(ValueError):self.first_apply(path)
        path.chmod(0o600)
        link=self.base/'review-link.json';link.symlink_to(path)
        with self.assertRaises(ValueError):self.first_apply(link)
        for _,_,paths in seeded:self.assertTrue(all(p.exists() for p in paths))

    def test_zero_one_two_three_four_hundred_and_real_deletion(self):
        for n in (0,1,2,3,4,100):
            with self.subTest(n=n):
                with self.db.transaction() as c:
                    c.execute('DELETE FROM gallery_versions');c.execute('DELETE FROM gallery_items')
                    c.execute('DELETE FROM generation_attempts')
                seeded=self.seed(n);before=self.financial()
                report=self.retention.run()
                self.assertEqual(report['candidate_results'],max(0,n-3))
                self.assertEqual(len(WorkGallery(self.db,self.settings.temp_dir).works('u',99).entries),min(3,n))
                done=self.apply();self.assertEqual(done['removed_versions'],max(0,n-3))
                for _v,_i,paths in seeded[-3:]:self.assertTrue(all(p.exists() for p in paths))
                for _v,_i,paths in seeded[:-3] if n>3 else []:self.assertTrue(all(not p.exists() for p in paths))
                self.assertEqual(before,self.financial())
                self.assertEqual(self.apply()['removed_files'],0)

    def test_corrections_each_occupy_one_slot_and_order_uses_completion(self):
        seeded=self.seed(8,same_item=True)
        with self.db.read() as c:self.assertEqual([r['id'] for r in visible_results(c,'u')],[r[0] for r in reversed(seeded[-3:])])
        done=self.apply();self.assertEqual(done['removed_versions'],5)
        with self.db.read() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM gallery_items').fetchone()[0],1)
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
        self.assertTrue(self.gallery.list_versions('u',seeded[-1][1])[-1].source_path.exists())

    def test_bounded_batches_converge_and_do_not_revisit_retired_stubs(self):
        seeded=self.seed(104)
        self.gallery.record_rating('u',seeded[0][0],2)
        self.assertEqual(self.retention.run()['candidate_results'],101)
        first=self.apply();self.assertEqual(first['candidate_results'],100)
        self.assertEqual(first['metadata_stubs_preserved'],1)
        second=self.apply();self.assertEqual(second['candidate_results'],1)
        self.assertEqual(self.apply()['candidate_results'],0)
        with self.db.read() as c:self.assertEqual(len(visible_results(c,'u')),3)

    def test_deleting_newer_expired_rows_does_not_resurface_old_paid_history(self):
        seeded=self.seed(5)
        with self.db.transaction() as c:
            c.execute("UPDATE gallery_versions SET unlock_status='unlocked',delivery_count=1,unlocked_at=? WHERE id=?",(NOW.isoformat(),seeded[0][0]))
            c.execute('UPDATE gallery_items SET retention_until=?',((NOW-timedelta(days=1)).isoformat(),))
        self.apply()
        self.assertTrue(seeded[0][2][1].exists())
        with self.db.read() as c:
            self.assertEqual(visible_results(c,'u'),[])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM generation_attempts WHERE status='succeeded'").fetchone()[0],5)

    def test_failed_and_processing_do_not_evict_successes(self):
        seeded=self.seed(5)
        with self.db.transaction() as c:
            c.execute("UPDATE gallery_versions SET status='failed' WHERE id=?",(seeded[-1][0],))
            c.execute("UPDATE gallery_versions SET status='processing' WHERE id=?",(seeded[-2][0],))
            c.execute("UPDATE generation_attempts SET status='failed_technical' WHERE id=?",('a-'+seeded[-1][0],))
            c.execute("UPDATE generation_attempts SET status='processing' WHERE id=?",('a-'+seeded[-2][0],))
        with self.db.read() as c:self.assertEqual([r['id'] for r in visible_results(c,'u')],[r[0] for r in reversed(seeded[:3])])
        self.assertEqual(self.retention.run()['candidate_results'],0)

    def test_dry_run_is_readonly_does_not_create_receipt_or_touch_db(self):
        self.seed(5)
        before=self.settings.database_path.read_bytes()
        with patch('app.result_retention.Database',side_effect=AssertionError('write constructor')):
            report=self.retention.run()
        self.assertEqual(report['candidate_results'],2)
        self.assertEqual(before,self.settings.database_path.read_bytes())
        self.assertFalse((self.settings.data_dir/'result_retention_last.json').exists())
        self.assertFalse((self.settings.data_dir/'result-retention.lock').exists())
        self.assertNotIn('u-v-',json.dumps(report));self.assertNotIn(str(self.base),json.dumps(report))

    def test_confirmed_original_is_protected_180_days_and_can_be_redelivered(self):
        seeded=self.seed(5)
        with self.db.transaction() as c:
            self.commerce.grant_continuation_pack(c,user_id='u',payment_order_id='synthetic-order',payment_intent_id=None)
        v=seeded[0][0];reservation=self.commerce.reserve_unlock_delivery('u',v)
        # A failed transport releases without consuming, exactly once.
        self.commerce.release_unlock_delivery('u',v,reservation.entitlement_id)
        self.commerce.release_unlock_delivery('u',v,reservation.entitlement_id)
        self.assertEqual(self.commerce.entitlement_balance('u').available,1)
        reservation=self.commerce.reserve_unlock_delivery('u',v)
        self.commerce.commit_unlock_delivery('u',v,reservation.entitlement_id)
        with self.db.transaction() as c:c.execute('UPDATE gallery_versions SET delivery_count=1,last_delivered_at=? WHERE id=?',(NOW.isoformat(),v))
        before=self.financial();report=self.apply()
        self.assertEqual(report['protection_reasons'].get('paid_retention'),1)
        self.assertTrue(reservation.original_path.exists())
        again=self.commerce.reserve_unlock_delivery('u',v);self.assertTrue(again.already_unlocked)
        self.assertEqual(before,self.financial())
        self.retention.clock=lambda:NOW+timedelta(days=181)
        # No second grant is revoked when media availability finally expires.
        self.apply();self.assertFalse(reservation.original_path.exists())
        self.assertEqual(before,self.financial())

    def test_feedback_survives_as_metadata_stub(self):
        seeded=self.seed(5);v=seeded[0][0]
        self.gallery.record_rating('u',v,2);self.gallery.record_feedback_message('u',v,'synthetic feedback')
        report=self.apply();self.assertEqual(report['metadata_stubs_preserved'],1)
        with self.db.read() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM user_feedback').fetchone()[0],2)
            self.assertIsNone(c.execute('SELECT original_path FROM gallery_versions WHERE id=?',(v,)).fetchone()[0])

    def test_shared_source_and_other_users_files_never_deleted(self):
        seeded=self.seed(5);foreign=self.seed(1,user='other')[0]
        with self.db.transaction() as c:c.execute('UPDATE gallery_versions SET source_path=? WHERE id=?',(str(seeded[0][2][0]),seeded[-1][0]))
        report=self.apply();self.assertGreater(report['shared_files_preserved'],0)
        self.assertTrue(seeded[0][2][0].exists())
        self.assertTrue(all(p.exists() for p in foreign[2]))

    def test_foreign_and_outside_paths_fail_closed(self):
        seeded=self.seed(5);foreign=self.seed(1,user='other')[0]
        # A reference to a foreign existing file is preserved as shared; an
        # unreferenced foreign file is not allowed to be deleted either.
        foreign_file=foreign[2][0].parent/'unreferenced.png';foreign_file.write_bytes(b'foreign')
        with self.db.transaction() as c:c.execute('UPDATE gallery_versions SET source_path=? WHERE id=?',(str(foreign_file),seeded[0][0]))
        self.assertGreater(self.retention.run()['path_anomaly_count'],0)
        with self.assertRaises(RuntimeError):self.apply()
        self.assertTrue(foreign_file.exists());self.assertTrue(seeded[0][2][1].exists())

    def test_reader_guard_defers_writer_without_blocking_other_readers(self):
        self.seed(5)
        with media_guard(self.settings.data_dir):
            with ThreadPoolExecutor(1) as pool:
                report=pool.submit(self.retention.run,execute=True).result(timeout=5)
            self.assertEqual(report['mode'],'busy')
            with ThreadPoolExecutor(1) as pool:
                def reader():
                    with media_guard(self.settings.data_dir):return True
                self.assertTrue(pool.submit(reader).result(timeout=5))
        self.assertEqual(self.apply()['removed_versions'],2)

    def test_pending_and_late_payment_targets_are_never_deleted(self):
        seeded=self.seed(5);v,item,paths=seeded[0]
        with self.db.transaction() as c:
            c.execute("""INSERT INTO payment_orders(id,public_token,attempt_id,version_id,user_id,
                provider,merchant_hash,provider_invoice_id,amount_minor,currency,status,description,
                created_at,updated_at,expires_at) VALUES('order','opaque','legacy',?,'u','robokassa',
                'synthetic',1,4900,'RUB','expired','synthetic',?,?,?)""",(v,NOW.isoformat(),NOW.isoformat(),NOW.isoformat()))
        before=self.financial();report=self.apply()
        self.assertEqual(report['protection_reasons']['payment_or_delivery_obligation'],1)
        self.assertEqual(before,self.financial());self.assertTrue(all(p.exists() for p in paths))

    def test_trash_waits_the_promised_period(self):
        seeded=self.seed(5);self.gallery.soft_delete('u',seeded[0][1])
        report=self.apply();self.assertEqual(report['protection_reasons']['trash_retention'],1)
        self.assertTrue(seeded[0][2][1].exists())

    def test_pending_instruction_without_version_preserves_its_sources(self):
        seeded=self.seed(5);store=MaxConversationStore(self.db,lambda:NOW)
        store.get_or_create('u','chat')
        store.transition('u','waiting_for_prompt',force=True,user_id='u',current_gallery_item_id=seeded[0][1],current_version_id=None)
        self.assertEqual(self.apply()['protection_reasons']['editing_dialog'],1)
        self.assertTrue(all(p.exists() for p in seeded[0][2]))
        store.transition('u','main_menu',force=True,current_gallery_item_id=None,current_version_id=None)
        self.apply();self.assertFalse(seeded[0][2][1].exists())

    def test_symlink_parent_is_refused(self):
        seeded=self.seed(5);directory=seeded[0][2][0].parent
        outside=self.base/'outside';outside.mkdir()
        link=directory/'link'
        try:link.symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('symlink privilege unavailable')
        target=outside/'foreign.png';target.write_bytes(b'private')
        with self.db.transaction() as c:c.execute('UPDATE gallery_versions SET source_path=? WHERE id=?',(str(link/'foreign.png'),seeded[0][0]))
        self.assertGreater(self.retention.run()['path_anomaly_count'],0)
        with self.assertRaises(RuntimeError):self.apply()
        self.assertTrue(target.exists())

    @skipUnless(os.name=='posix','real ancestor-swap protection is verified on Linux CI')
    def test_root_ancestor_swap_after_validation_is_refused(self):
        ancestor=self.base/'ancestor';root=ancestor/'users';root.mkdir(parents=True)
        target=root/'image.png';target.write_bytes(b'original')
        identity=_safe_metadata(target,root)
        moved=self.base/'moved'
        def swap(*args):
            ancestor.rename(moved)
            ancestor.symlink_to(moved,target_is_directory=True)
            return identity
        try:
            with patch('app.result_retention._safe_metadata',side_effect=swap):
                with self.assertRaises(OSError):_unlink(target,root,identity)
            self.assertEqual((moved/'users'/'image.png').read_bytes(),b'original')
        finally:
            if ancestor.is_symlink():ancestor.unlink()

    def service(self):
        self.tick=NOW
        settings=replace(self.settings,demo_max_successful_generations=20,demo_min_request_interval_seconds=1)
        provider=FakeImageProvider()
        return DemoService(settings,self.db,self.storage,WatermarkService('ОБРАЗЕЦ',1024,'JPEG',82),provider,
                           clock=lambda:self.tick),provider

    def test_real_corrections_replay_and_failed_delivery_preserve_attempt_proof(self):
        service,provider=self.service();session=service.start_session('max','u',self.source)
        with self.db.transaction() as c:self.commerce.adjust_generation_credits(c,user_id='u',delta=8,reason='fixture',idempotency_key='fixture')
        results=[];parent=None
        for n in range(4):
            self.tick+=timedelta(seconds=3)
            result=service.generate(session.session_id,'synthetic edit',f'event-{n}',correction=n>0,parent_version_id=parent)
            results.append(result)
            with self.db.read() as c:parent=c.execute('SELECT id FROM gallery_versions WHERE attempt_id=?',(result.attempt_id,)).fetchone()[0]
        before=self.financial();self.apply();self.assertEqual(before,self.financial())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM generation_attempts WHERE status='succeeded'").fetchone()[0],4)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM generation_credit_reservations WHERE status='consumed'").fetchone()[0],4)
        calls=provider.calls
        with self.assertRaises(InvalidInputError):service.generate(session.session_id,'synthetic edit','event-0')
        self.assertEqual(provider.calls,calls);self.assertEqual(before,self.financial())
        self.tick+=timedelta(seconds=3)
        with self.assertRaises(DeliveryError):service.generate(session.session_id,'synthetic edit','failed-delivery',delivery_override=lambda *_:False)
        with self.db.read() as c:self.assertEqual(len(visible_results(c,'u')),3)
        self.assertEqual(self.commerce.balance('u').reserved,0)
        self.tick+=timedelta(seconds=3)
        service.generate(session.session_id,'synthetic correction after cleanup','after-cleanup',correction=True,parent_version_id=parent)
        with self.db.read() as c:self.assertEqual(len(visible_results(c,'u')),3)

    def test_referral_v2_proof_survives_eviction_and_reward_is_not_duplicated(self):
        service,_provider=self.service();referrals=ReferralService(self.db,self.commerce,v2_enabled=True,clock=lambda:NOW)
        code=referrals.get_or_create_code('u');relationship=referrals.register_start('friend',code,event_key='friend-start')
        session=service.start_session('max','friend',self.source)
        with self.db.transaction() as c:self.commerce.adjust_generation_credits(c,user_id=session.user_id,delta=4,reason='fixture',idempotency_key='fixture-friend')
        first=None
        for n in range(4):
            self.tick+=timedelta(seconds=3)
            r=service.generate(session.session_id,'synthetic edit',f'friend-event-{n}')
            first=first or r.attempt_id
            if n==0:self.assertIsNotNone(referrals.reward_first_success(session.user_id))
        before=self.financial();self.apply();self.assertEqual(before,self.financial())
        self.assertIsNone(referrals.reward_first_success(session.user_id))
        self.assertEqual(self.commerce.balance('u').available,6)
        self.assertEqual(self.commerce.entitlement_balance('u').available,2)
        self.assertEqual(self.commerce.entitlement_balance(session.user_id).available,1)
        with self.db.read() as c:
            self.assertEqual(c.execute('SELECT status FROM generation_attempts WHERE id=?',(first,)).fetchone()[0],'succeeded')
            self.assertEqual(c.execute('SELECT first_success_attempt_id FROM referral_relationships WHERE id=?',(relationship,)).fetchone()[0],first)

    def test_interrupted_unlink_rolls_back_metadata_and_retry_finishes(self):
        seeded=self.seed(5);before=self.financial();count=0
        def fail_after_one(*args):
            nonlocal count
            fixture_unlink(*args);count+=1
            if count==1:raise ConnectionError('synthetic interruption')
        with patch('app.result_retention._unlink',side_effect=fail_after_one):
            with self.assertRaises(ConnectionError):self.retention.run(execute=True)
        with self.db.read() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM gallery_versions').fetchone()[0],5)
        done=self.apply();self.assertGreater(done['already_missing_files'],0)
        self.assertEqual(before,self.financial())
        self.assertTrue(all(p.exists() for r in seeded[-3:] for p in r[2]))

    @skipUnless(os.name=='posix','real fd-relative deletion/crash is verified on Linux CI')
    def test_process_crash_and_restart(self):
        seeded=self.seed(5);before=self.financial()
        result=subprocess.run([sys.executable,'-m','tests.test_result_retention','--crash',str(self.base)],capture_output=True)
        self.assertEqual(result.returncode,73,result.stderr.decode(errors='replace'))
        self.assertGreater(self.apply()['already_missing_files'],0)
        self.assertEqual(before,self.financial())
        self.assertTrue(all(p.exists() for r in seeded[-3:] for p in r[2]))


if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='--crash':
    settings=Settings('','fake','test',Path(sys.argv[2]))
    def crash(path,root,identity):
        _unlink(path,root,identity);os._exit(73)
    with patch('app.result_retention._unlink',side_effect=crash):
        ResultRetention(settings,clock=lambda:NOW).run(execute=True)
