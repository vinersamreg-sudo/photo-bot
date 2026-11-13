import hashlib
import json
import multiprocessing
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch

from app.backup import BackupError, BackupManager
from app.database import Database
from app.ru_db_recovery import _prove, publish as checkpoint_publish, restore_latest
from app.ru_recovery import READBACK, S3, _read_point, _restore, _scheduled_day, bounded_scratch, promote, watchdog
from app.ru_recovery_gate import gate
from app.ru_recovery_notify import notify
from app.ru_recovery_publication import recover_interrupted_writer

PHRASE = "fixture-only recovery passphrase"
START = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)


def empty(db=False):
    return {"version": 2, "points": [], "pending": None, "retired": []} if db else {
        "version": 3, "points": [], "scheduled_days": {}, "pending": None, "retired": []}


class MemoryStore:
    def __init__(self, db=False):
        self.catalog = empty(db)
        self.objects = {}
        self.corrupt = False
        self.fail_commit = False
        self.fail_delete = False
        self.etag = 0

    def get_catalog(self, _work):
        return self.catalog, self.etag

    def put_catalog(self, catalog, etag, _work):
        if etag != self.etag or (self.fail_commit and catalog["points"] != self.catalog["points"]):
            raise BackupError("conditional write rejected")
        self.catalog = catalog
        self.etag += 1

    def put(self, key, path):
        if key in self.objects:
            raise BackupError("create-only")
        self.objects[key] = path.read_bytes()

    def get(self, key, path, expected_size):
        raw = self.objects[key]
        if len(raw) != expected_size:
            raise BackupError("size mismatch")
        if self.corrupt and key == self.catalog["pending"]:
            raw = b"x" * len(raw)
        path.write_bytes(raw)

    def discard(self, key):
        if self.fail_delete:
            raise BackupError("synthetic delete failed")
        self.objects.pop(key, None)

    def abort_unfinished(self, key):
        pass


class SharedStore:
    """Local durable S3 contract emulator; each client uses its own process and connection."""
    def __init__(self, file, barrier=None):
        self.file, self.barrier = file, barrier

    def get_catalog(self, _work):
        with closing(sqlite3.connect(self.file)) as c:
            etag, payload = c.execute("SELECT revision,body FROM catalog").fetchone()
        if self.barrier is not None:
            self.barrier.wait(25)
            self.barrier = None
        return json.loads(payload), etag

    def put_catalog(self, catalog, etag, _work):
        with closing(sqlite3.connect(self.file)) as c, c:
            if not c.execute("UPDATE catalog SET revision=revision+1,body=? WHERE revision=?",
                             (json.dumps(catalog),etag)).rowcount:
                raise BackupError("conditional write rejected")

    def put(self, key, path):
        with closing(sqlite3.connect(self.file)) as c, c:
            try:
                c.execute("INSERT INTO objects VALUES(?,?)",(key,path.read_bytes()))
            except sqlite3.IntegrityError:
                raise BackupError("create-only") from None

    def get(self, key, path, expected_size):
        with closing(sqlite3.connect(self.file)) as c:
            row = c.execute("SELECT body FROM objects WHERE key=?",(key,)).fetchone()
        if row is None or len(row[0]) != expected_size:
            raise BackupError("size mismatch")
        path.write_bytes(row[0])

    def discard(self, key):
        with closing(sqlite3.connect(self.file)) as c, c:
            c.execute("DELETE FROM objects WHERE key=?",(key,))


def race_writer(store_path, barrier, result, source_db, archive_source, work, db):
    try:
        store = SharedStore(store_path, barrier)
        work.mkdir()
        if db:
            archive = work / "sqlite.enc"
            shutil.copyfile(archive_source,archive)
            checkpoint_publish(store,work,archive,START+timedelta(days=2),
                               lambda p: _prove(p,work/"proof.sqlite3",PHRASE))
        else:
            archive = work / READBACK
            shutil.copyfile(archive_source,archive)
            manager = BackupManager(source_db,work,14)
            promote(store,work,archive,lambda h: _restore(manager,READBACK,PHRASE,work/"restore",h),
                    scheduled="2026-11-03",now=lambda:START+timedelta(days=2))
        result.put("PASS")
    except BackupError as exc:
        result.put(str(exc))
    except Exception as exc:
        result.put(type(exc).__name__)


class RuRecoveryTests(TestCase):
    def small_publish(self, store, root, data, day):
        archive = root / READBACK
        archive.write_bytes(data)
        instant = datetime.fromisoformat(day + "T03:00:00+00:00")
        return promote(store, root, archive, lambda _: {"state_sha256": hashlib.sha256(data).hexdigest()},
                       scheduled=day, now=lambda: instant)

    def test_verification_cas_and_delete_failures_preserve_live_points(self):
        with TemporaryDirectory() as d:
            root, store = Path(d), MemoryStore()
            for i in range(1, 7):
                self.small_publish(store, root, bytes([i]), f"2026-11-0{i}")
            old = store.catalog["points"]
            count = len(old)
            self.assertEqual(count,5)
            store.corrupt = True
            with self.assertRaisesRegex(BackupError,"readback"):
                self.small_publish(store,root,b"bad","2026-11-07")
            self.assertEqual(store.catalog["points"],old)
            self.assertEqual(len(store.objects),count)
            store.corrupt, store.fail_commit = False, True
            with self.assertRaisesRegex(BackupError,"conditional"):
                self.small_publish(store,root,b"ok","2026-11-07")
            self.assertEqual(store.catalog["points"],old)
            self.assertEqual(len(store.objects),count+1)
            from app.ru_recovery import _validate_points
            recover_interrupted_writer(store,root,_validate_points,store.catalog["pending"])
            self.assertEqual(len(store.objects),count)
            store.fail_commit, store.fail_delete = False, True
            with self.assertRaisesRegex(BackupError,"delete failed"):
                self.small_publish(store,root,b"ok","2026-11-07")
            self.assertEqual(len(store.objects),count+1)
            self.assertIsNone(store.catalog["pending"])
            self.assertEqual(len(store.catalog["retired"]),1)
            new = store.catalog["points"]
            with self.assertRaises(BackupError):
                self.small_publish(store,root,b"next","2026-11-08")
            self.assertEqual(store.catalog["points"],new)
            self.assertEqual(len(store.objects),count+1)
            store.fail_delete = False
            self.small_publish(store,root,b"next","2026-11-08")
            self.assertEqual(len(store.objects),count)

    def test_daily_and_weekly_labels_deduplicate_and_watchdog_restores(self):
        with TemporaryDirectory() as d:
            root, store = Path(d), MemoryStore()
            for i in range(20):
                instant = START + timedelta(days=i)
                payload = bytes([min(i,18)])
                self.small_publish(store,root,payload,instant.date().isoformat())
                self.assertLessEqual(len(store.objects),6)
            days = store.catalog["scheduled_days"]
            self.assertEqual(set(days),{"2026-11-08","2026-11-15","2026-11-17","2026-11-18","2026-11-19","2026-11-20"})
            self.assertEqual(days["2026-11-19"],days["2026-11-20"])
            self.assertEqual(len(store.objects),5)
            prove = lambda _: {"state_sha256": hashlib.sha256(payload).hexdigest()}
            watchdog(store,root,prove,instant.replace(hour=6))
            with self.assertRaises(BackupError):
                watchdog(store,root,prove,instant.replace(hour=6)+timedelta(days=1))

    def test_checkpoint_cannot_replace_a_point_on_bad_readback(self):
        with TemporaryDirectory() as d:
            root, store = Path(d), MemoryStore(db=True)
            archive = root / "sqlite.enc"
            for i in range(2):
                archive.write_bytes(bytes([i]))
                checkpoint_publish(store,root,archive,START+timedelta(minutes=15*i),lambda _: None)
            before = store.catalog["points"]
            store.corrupt = True
            with self.assertRaises(BackupError):
                checkpoint_publish(store,root,archive,START+timedelta(minutes=30),lambda _: None)
            self.assertEqual(store.catalog["points"],before)
            self.assertEqual(len(store.objects),2)

    def test_lost_commit_response_never_deletes_a_published_archive(self):
        with TemporaryDirectory() as d:
            root, store = Path(d), MemoryStore()
            self.small_publish(store,root,b"old","2026-11-01")
            put = store.put_catalog

            def lose_response(catalog, etag, work):
                changed = catalog["points"] != store.catalog["points"]
                put(catalog,etag,work)
                if changed:
                    raise TimeoutError("synthetic lost commit response")

            with patch.object(store,"put_catalog",side_effect=lose_response):
                with self.assertRaises(TimeoutError):
                    self.small_publish(store,root,b"new","2026-11-02")
            self.assertIsNone(store.catalog["pending"])
            self.assertEqual(len(store.catalog["points"]),2)
            for point in store.catalog["points"]:
                _read_point(store,point,root/READBACK)

    def test_unconfirmed_upload_keeps_the_writer_fence(self):
        from app.ru_recovery_publication import UncertainMultipart

        with TemporaryDirectory() as d:
            root, store = Path(d), MemoryStore()
            self.small_publish(store,root,b"old","2026-11-01")
            with patch.object(store,"put",side_effect=UncertainMultipart("unknown upload")):
                with self.assertRaises(UncertainMultipart):
                    self.small_publish(store,root,b"new","2026-11-02")
            self.assertIsNotNone(store.catalog["pending"])
            self.assertEqual(len(store.catalog["points"]),1)
            with self.assertRaisesRegex(BackupError,"Another writer"):
                self.small_publish(store,root,b"retry","2026-11-02")

    def test_interrupted_writer_blocks_until_fenced_and_cleared(self):
        from app.ru_recovery import _validate_points
        from app.ru_recovery_publication import reserve

        with TemporaryDirectory() as d:
            root, store = Path(d), MemoryStore()
            self.small_publish(store,root,b"old","2026-11-01")
            class Crash(BaseException):
                pass
            with self.assertRaises(Crash), reserve(store,root,_validate_points) as txn:
                archive = root / "partial.enc"
                archive.write_bytes(b"private")
                store.put(txn.key,archive)
                raise Crash
            pending = store.catalog["pending"]
            with self.assertRaises(BackupError):
                self.small_publish(store,root,b"new","2026-11-02")
            with self.assertRaises(BackupError):
                recover_interrupted_writer(store,root,_validate_points,"a"*32)
            recover_interrupted_writer(store,root,_validate_points,pending)
            self.assertEqual(len(store.objects),1)
            self.small_publish(store,root,b"new","2026-11-02")
            self.assertEqual(len(store.objects),2)

    def test_s3_sends_yandex_conditions_and_aborts_interrupted_multipart(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            small, large = root/"small", root/"large"
            small.write_bytes(b"contents")
            with large.open("wb") as f:
                f.truncate(64*1024**2+1)
            store = S3("https://storage.yandexcloud.net","synthetic-bucket","ru-central1")
            key = "a"*32
            with patch.object(store,"_api",return_value={}) as api:
                store.put(key,small)
                store.put_catalog(empty(),None,root)
                store.put_catalog(empty(),"etag",root)
            calls = [c.args for c in api.call_args_list]
            self.assertEqual([c[-2:] for c in calls],[("--if-none-match","*"),
                             ("--if-none-match","*"),("--if-match","etag")])
            with patch.object(store,"_api",side_effect=[{"UploadId":"x"},{"ETag":"e1"},{"ETag":"e2"},{}]) as api:
                store.put(key,large)
            self.assertEqual(api.call_args.args[0],"complete-multipart-upload")
            self.assertEqual(api.call_args.args[-2:],("--if-none-match","*"))
            with patch.object(store,"_api",side_effect=[{"UploadId":"x"},BackupError("network"),{}]) as api:
                with self.assertRaisesRegex(BackupError,"network"):
                    store.put(key,large)
            self.assertEqual(api.call_args.args[0],"abort-multipart-upload")
            rule = {"Status":"Enabled","Filter":{"Prefix":"ravuna/full-v3/objects/"},
                    "AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}
            with patch.object(store,"_api",side_effect=[{},{"Rules":[rule]}]):
                store.ensure_bounded()
            with patch.object(store,"_api",side_effect=[{},{"Rules":[rule,{"Status":"Enabled","Expiration":{"Days":1}}]}]):
                with self.assertRaises(BackupError):
                    store.ensure_bounded()

    def test_scheduler_and_scratch_and_notification_acknowledgement(self):
        units = Path(__file__).resolve().parents[1] / "ops/ru"
        daily = (units / "ravuna-ru-recovery.service").read_text(encoding="utf-8")
        self.assertIn("ProtectSystem=strict",daily)
        self.assertIn("ReadWritePaths=/opt/photo-bot/data/result-retention.lock",daily)
        self.assertIn("OnSuccess=ravuna-ru-recovery-ok@%n.service",daily)
        self.assertIn("OnFailure=ravuna-ru-recovery-fail@%n.service",daily)
        start = datetime(2026,11,11,23,0,10,tzinfo=timezone.utc)
        trigger = int(start.timestamp()-5)*1_000_000
        with patch("app.ru_recovery.subprocess.run",return_value=SimpleNamespace(stdout="t 0")):
            self.assertIsNone(_scheduled_day(start))
        with patch("app.ru_recovery.subprocess.run",return_value=SimpleNamespace(stdout=f"t {trigger}")):
            self.assertEqual(_scheduled_day(start),"2026-11-12")
        with TemporaryDirectory() as d:
            root = Path(d)
            leftover = root/".ravuna-recovery-full-run"
            leftover.mkdir(mode=0o700)
            (leftover/".run-owned").write_bytes(b"ravuna-recovery-scratch-v1")
            (leftover/"interrupted").write_bytes(b"data")
            with bounded_scratch(root,"full-run") as work:
                self.assertFalse((work/"interrupted").exists())
            self.assertFalse(leftover.exists())
            (root/"notify.url").write_text("https://receiver.invalid/health",encoding="ascii")
            (root/"notify.token").write_text("fixture-only-invalid-credential",encoding="ascii")
            with patch("app.ru_recovery_notify.build_opener") as builder:
                call = builder.return_value.open
                call.return_value.__enter__.return_value.status = 503
                with self.assertRaises(OSError):
                    notify("fail","ravuna-ru-db-recovery.service",root)
                call.return_value.__enter__.return_value.status = 204
                notify("ok","ravuna-ru-recovery.service",root)
            self.assertEqual(json.loads(call.call_args.args[0].data)["state"],"ok")

    def fixture(self):
        from tests.test_payments import PaymentTests
        p = PaymentTests()
        self.addCleanup(p.doCleanups)
        p.setUp()
        (p.base/".deploy-sha").write_text("a"*40,encoding="ascii")
        return p

    def test_recovery_finance_checks_refunds_spend_and_account_mapping(self):
        from app.commerce import MEDIUM_PACKAGE, SMALL_PACKAGE
        from app.robokassa import RobokassaRefundResult
        from app.ru_recovery_consistency import reconcile_provider

        p = self.fixture()
        start = p.clock() - timedelta(minutes=1)
        orders = [p.service.create_order(p.user_id,None,"recovery-finance-"+pack.code,
                  account_purchase=True,product_code=pack.code)
                  for pack in (SMALL_PACKAGE, MEDIUM_PACKAGE)]
        for order in orders:
            fields = p.signed_callback(order)
            fields["OpKey"] = "synthetic-operation"
            self.assertTrue(p.service.process_webhook(fields,method="POST",
                            path="/payments/robokassa/result").accepted)
        p.clock.advance(minutes=1)
        p.demo.generate(p.session.session_id,"synthetic correction","recovery-spend",correction=True)
        self.assertEqual(p.demo.commerce.balance(p.user_id).available,23)

        class RefundProvider:
            name = "robokassa"
            merchant_login = "ravuna-test"

            def create_refund(self, request):
                return RobokassaRefundResult(True,"synthetic-refund",None,200)

            def refund_status(self, request_id):
                return "finished",200

        p.service.provider = RefundProvider()
        object.__setattr__(p.settings,"payment_refunds_enabled",True)
        medium = orders[1]
        refund = p.service.prepare_refund(medium.id,medium.amount_minor,"customer_request","recovery-refund")
        p.service.submit_refund(refund.id)
        for _ in range(2):
            self.assertEqual(p.service.refresh_refund(refund.id).status.value,"succeeded")
        end = p.clock() + timedelta(minutes=1)
        evidence = {"coverage_start":start.isoformat(),"coverage_end":end.isoformat(),
                    "operations":[{"invoice":order.provider_invoice_id,"amount_minor":order.amount_minor,
                                   "refunded_minor":order.amount_minor if order==medium else 0,
                                   "currency":"RUB"} for order in orders]}
        with TemporaryDirectory() as d:
            db = Path(d)/"restored.sqlite3"
            BackupManager(p.settings.database_path,Path(d),14)._sqlite_snapshot(db,standalone=True)
            def reconcile():
                return reconcile_provider(db,evidence,from_time=start,through_time=end)
            self.assertEqual(reconcile()["matched_provider_operations"],2)
            with closing(sqlite3.connect(db)) as c, c:
                c.execute("UPDATE user_credit_accounts SET available_generation_credits=2 WHERE user_id=?",
                          (p.user_id,))
            with self.assertRaisesRegex(BackupError,"ledger"):
                reconcile()
            with closing(sqlite3.connect(db)) as c, c:
                c.execute("UPDATE user_credit_accounts SET available_generation_credits=1 WHERE user_id=?",
                          (p.user_id,))
                c.execute("DELETE FROM payment_receipts WHERE receipt_type='refund'")
            with self.assertRaisesRegex(BackupError,"refund ledger"):
                reconcile()

    @skipUnless(shutil.which("openssl"),"OpenSSL CLI required")
    def test_two_process_race_every_published_media_and_db_point_restores(self):
        ctx = multiprocessing.get_context("spawn")
        p = self.fixture()
        source = p.settings.database_path
        with TemporaryDirectory() as d:
            root = Path(d)
            for db in (False,True):
                kind = "sqlite" if db else "media"
                stage = root/kind; stage.mkdir()
                storage = stage/"store.sqlite3"
                with closing(sqlite3.connect(storage)) as c, c:
                    c.executescript("CREATE TABLE catalog(revision INTEGER,body TEXT); CREATE TABLE objects(key TEXT PRIMARY KEY,body BLOB);")
                    c.execute("INSERT INTO catalog VALUES(0,?)",(json.dumps(empty(db)),))
                store = SharedStore(storage)
                archives = []
                for i in range(3):
                    work = stage/f"seed-{i}"; work.mkdir()
                    p.service.create_order(p.user_id, p.versions[i%2]["id"],
                                           f"race-{kind}-{i}", account_purchase=False)
                    p.clock.advance(minutes=31)
                    manager = BackupManager(source,work,14)
                    if db:
                        meta = manager.create(PHRASE, _standalone_sqlite=True)
                        archive = work/meta["backup_name"]
                        if i<2:
                            checkpoint_publish(store,work,archive,START+timedelta(days=i),
                                lambda path: _prove(path,work/"proof.sqlite3",PHRASE))
                    else:
                        meta = manager.create_recovery_bundle(PHRASE, _standalone_sqlite=True)
                        archive = work/meta["recovery_bundle_name"]
                        if i<2:
                            instant = START+timedelta(days=i)
                            promote(store,work,archive,lambda h: _restore(manager,archive.name,PHRASE,work/"proof",h),
                                    scheduled=instant.date().isoformat(),now=lambda:instant)
                    archives.append(archive)
                barrier, result = ctx.Barrier(2), ctx.Queue()
                procs = [ctx.Process(target=race_writer,args=(storage,barrier,result,source,archives[2],
                         stage/f"writer-{i}",db)) for i in range(2)]
                try:
                    for proc in procs:
                        proc.start()
                    outcomes = [result.get(timeout=55) for _ in procs]
                    for proc in procs:
                        proc.join(10)
                        self.assertEqual(proc.exitcode,0)
                    self.assertCountEqual(outcomes,["PASS","conditional write rejected"])
                finally:
                    for proc in procs:
                        if proc.is_alive():
                            proc.terminate()
                            proc.join(10)
                    result.close()
                index,_ = store.get_catalog(stage)
                self.assertIsNone(index["pending"])
                self.assertEqual(len(index["points"]),2 if db else 3)
                for i,point in enumerate(index["points"]):
                    work = stage/f"restore-{i}"; work.mkdir()
                    target = work/("db.enc" if db else READBACK)
                    if db:
                        from app.recovery_retention import regular_file_identity
                        _read_point(store,point,target,regular_file_identity)
                        _prove(target,work/"restored.sqlite3",PHRASE)
                    else:
                        _read_point(store,point,target)
                        evidence = _restore(BackupManager(source,work,14),target.name,PHRASE,
                                            work/"restored",point["sha256"])
                        self.assertGreater(evidence["distinct_media"],0)

    @skipUnless(shutil.which("openssl"),"OpenSSL CLI required")
    def test_wal_checkpoint_new_media_and_late_payment_block_reopen_then_reconcile(self):
        p = self.fixture()
        p.clock.value = datetime.now(timezone.utc)
        source = p.settings.database_path
        order = p.service.create_order(p.user_id,None,"late-after-full",account_purchase=True)
        with TemporaryDirectory() as d, closing(sqlite3.connect(source)) as reader:
            root = Path(d)
            reader.execute("PRAGMA journal_mode=WAL")
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM payment_orders").fetchone()
            manager = BackupManager(source,root,14)
            full = manager.create_recovery_bundle(PHRASE, _standalone_sqlite=True)
            restore = root/"full"
            proof = _restore(manager,full["recovery_bundle_name"],PHRASE,restore,full["sha256"])
            p.clock.advance(minutes=31)
            cb = p.signed_callback(order)
            for duplicate in (False, True):
                accepted = p.service.process_webhook(cb,method="POST",path="/payments/robokassa/result")
                self.assertTrue(accepted.accepted)
                self.assertEqual(accepted.duplicate,duplicate)
            self.assertIsNone(reader.execute("SELECT paid_at FROM payment_orders").fetchone()[0])
            cp = manager.create(PHRASE, _standalone_sqlite=True)
            store = MemoryStore(db=True)
            snapshot_time = datetime.now(timezone.utc)
            checkpoint_publish(store,root,root/cp["backup_name"],snapshot_time,
                               lambda path: _prove(path,root/"checked.sqlite3",PHRASE))
            checkpoint = root/"checkpoint.sqlite3"
            receipt = restore_latest(store,root,checkpoint,PHRASE)
            incident = p.clock() + timedelta(minutes=1)
            evidence = {"coverage_start":p.clock.value.replace(year=2025).isoformat(),
                        "coverage_end":incident.isoformat(),
                        "operations":[{"invoice":order.provider_invoice_id,"amount_minor":order.amount_minor,
                                       "refunded_minor":0,"currency":"RUB"}]}
            with self.assertRaisesRegex(BackupError,"provider|Provider"):
                gate(restore,manager.users_dir,proof["state_sha256"],incident,evidence)
            args = dict(checkpoint=checkpoint,checkpoint_sha256=receipt["sqlite_sha256"],
                        checkpoint_started=snapshot_time)
            verified = gate(restore,manager.users_dir,proof["state_sha256"],incident,evidence,**args)
            self.assertEqual(verified["matched_provider_operations"],1)
            self.assertEqual(verified["publication"],"NOT_AUTHORIZED")
            sidecar = checkpoint.with_name(checkpoint.name + "-wal")
            sidecar.write_bytes(b"unexpected")
            with self.assertRaisesRegex(BackupError,"sidecar"):
                gate(restore,manager.users_dir,proof["state_sha256"],incident,evidence,**args)
            sidecar.unlink()
            evidence["operations"].append({"invoice":order.provider_invoice_id+1,"amount_minor":4900,
                                           "refunded_minor":0,"currency":"RUB"})
            with self.assertRaisesRegex(BackupError,"no recovered order/account"):
                gate(restore,manager.users_dir,proof["state_sha256"],incident,evidence,**args)
            evidence["operations"].pop()
            with closing(sqlite3.connect(checkpoint)) as c, c:
                c.execute("UPDATE gallery_versions SET secondary_source_path=? WHERE id=?",
                          (str(manager.users_dir/"unavailable-correction-input.png"),p.versions[0]["id"]))
            args["checkpoint_sha256"] = BackupManager._sha256(checkpoint)
            with self.assertRaisesRegex(BackupError,"media absent"):
                gate(restore,manager.users_dir,proof["state_sha256"],incident,evidence,**args)
            with closing(sqlite3.connect(source)) as c, c:
                c.execute("UPDATE gallery_versions SET secondary_source_path=? WHERE id=?",
                          (str(manager.users_dir/"unavailable-correction-input.png"),p.versions[0]["id"]))
            missing = manager.create_recovery_bundle(PHRASE)
            with self.assertRaisesRegex(BackupError,"media absent"):
                _restore(manager,missing["recovery_bundle_name"],PHRASE,root/"missing",missing["sha256"])
            reader.rollback()
