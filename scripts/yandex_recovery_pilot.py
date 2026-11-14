"""Synthetic-only Yandex S3 pilot. By default prints a plan; apply needs explicit GO."""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import HTTPSHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backup import BackupError, BackupManager
from app.ru_db_recovery import _prove, publish as publish_db, restore_latest
from app.ru_recovery import READBACK, S3, _read_point, _restore, _validate_points, promote
from app.ru_recovery_notify import NoRedirect, notify
from app.ru_recovery_publication import control, recover_interrupted_writer, reserve

ENDPOINT = "https://storage.yandexcloud.net"
REGION = "ru-central1"
FULL, DB = "ravuna/full-v3", "ravuna/db-v2"
STAMP = datetime(2026, 11, 14, tzinfo=timezone.utc)


class ApiError(BackupError):
    def __init__(self, code):
        super().__init__("S3 command rejected")
        self.code = code


class PilotStore(S3):
    def __init__(self, bucket, profile, *, checkpoints=False, barrier=None):
        super().__init__(ENDPOINT, bucket, REGION, checkpoints=checkpoints)
        self.cli[1:1] = ["--profile", profile]
        self.barrier = barrier

    def _run(self, *args):
        env = {k: v for k, v in os.environ.items()
               if k not in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                            "AWS_SECURITY_TOKEN", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE",
                            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI"}}
        env.update(AWS_MAX_ATTEMPTS="1", AWS_EC2_METADATA_DISABLED="true", AWS_PAGER="",
                   AWS_REQUEST_CHECKSUM_CALCULATION="when_required",
                   AWS_RESPONSE_CHECKSUM_VALIDATION="when_required")
        p = subprocess.run([*self.cli, *args], env=env, capture_output=True, timeout=120, check=False)
        if p.returncode:
            match = re.search(rb"An error occurred \(([A-Za-z0-9]+)\)", p.stderr)
            raise ApiError(match[1].decode("ascii") if match else "CLIError")
        return p.stdout

    def get_catalog(self, work):
        result = super().get_catalog(work)
        if self.barrier is not None:
            self.barrier.wait(150)
            self.barrier = None
        return result


def bucket_policy(bucket, writer, db_writer, reader):
    arn = "arn:aws:s3:::" + bucket
    rules = []

    def rule(effect, who, actions, resource, condition=None):
        entry = {"Effect": effect, "Principal": "*" if who == "*" else {"CanonicalUser": who},
                 "Action": actions, "Resource": resource}
        if condition:
            entry["Condition"] = condition
        rules.append(entry)

    rule("Deny", "*", "s3:*", [arn, arn+"/*"], {"Bool":{"aws:SecureTransport":"false"}})
    for prefix, principal in ((FULL, writer), (DB, db_writer)):
        catalog, objects = f"{arn}/{prefix}/catalog.json", f"{arn}/{prefix}/objects/*"
        targets = [catalog, objects]
        rule("Allow", principal, ["s3:GetObject","s3:PutObject"], targets)
        rule("Allow", principal, ["s3:DeleteObject","s3:AbortMultipartUpload"], objects)
        rule("Allow", principal, ["s3:GetBucketVersioning","s3:GetLifecycleConfiguration"], arn)
        rule("Allow", principal, "s3:ListBucket", arn,
             {"StringEquals":{"s3:prefix":prefix+"/catalog.json"}})
        rule("Allow", principal, "s3:ListBucketMultipartUploads", arn)
        rule("Allow", reader, "s3:GetObject", targets)
        rule("Allow", reader, "s3:ListBucket", arn,
             {"StringEquals":{"s3:prefix":prefix+"/catalog.json"}})
        rule("Deny", "*", "s3:PutObject", objects, {"Null":{"s3:if-none-match":"true"}})
        rule("Deny", "*", "s3:PutObject", catalog,
             {"Null":{"s3:if-none-match":"true","s3:if-match":"true"}})
    rule("Allow", reader, ["s3:GetBucketVersioning","s3:GetLifecycleConfiguration"], arn)
    return {"Version":"2025-12-24","Statement":rules}


def put_json(admin, method, option, content, work):
    path = work/"configuration.json"
    path.write_text(json.dumps(content), encoding="ascii")
    return admin._api(method,"--bucket",admin.bucket,option,"file://"+str(path))


def expect_denied(call, *codes):
    try:
        call()
    except ApiError as exc:
        if exc.code in codes:
            return
        raise
    raise BackupError("Provider unexpectedly allowed a forbidden operation")


def check_identity(admin, owner):
    tags = admin._api("get-bucket-tagging","--bucket",admin.bucket).get("TagSet",[])
    if tags != [{"Key":"RavunaRecoveryPilot","Value":owner}]:
        raise BackupError("No ownership proof for this disposable bucket")


def cleanup(admin, owner):
    check_identity(admin,owner)
    args = ("--bucket",admin.bucket)
    remote = admin._api("list-objects-v2",*args,"--max-keys","30","--no-paginate")
    pending = admin._api("list-multipart-uploads",*args,"--max-uploads","20","--no-paginate")
    if remote.get("IsTruncated") or pending.get("IsTruncated"):
        raise BackupError("Unexpected pilot storage count; cleanup refused")
    pattern = r"ravuna/(?:full-v3|db-v2)/(?:catalog\.json|objects/[a-f0-9]{32}\.enc)"
    objects = [o["Key"] for o in remote.get("Contents",[])]
    uploads = pending.get("Uploads",[])
    if any(not re.fullmatch(pattern,x) for x in objects + [u["Key"] for u in uploads]):
        raise BackupError("Unexpected key in pilot bucket; cleanup refused")
    # Disable all bucket-scoped writer/reader grants before draining objects.
    admin._api("delete-bucket-policy",*args)
    for u in uploads:
        admin._api("abort-multipart-upload",*args,"--key",u["Key"],"--upload-id",u["UploadId"])
    for name in objects:
        admin._api("delete-object",*args,"--key",name)
    admin._api("delete-bucket",*args)
    expect_denied(lambda: admin._api("head-bucket",*args),"404","NotFound","NoSuchBucket")


def fixture(work):
    # No input paths or customer data accepted. The existing payment fixtures
    # use synthetic pictures and signed callbacks without network calls.
    from PIL import Image
    from tests.test_payments import PaymentTests
    f = PaymentTests()
    f.setUp()
    try:
        (f.base/".deploy-sha").write_text("a"*40,encoding="ascii")
        second = work/"synthetic-second.png"
        Image.new("RGB",(100,100),"green").save(second)
        secondary = f.demo.add_secondary_source(f.session.session_id,second)
        pending = f.pending_request("pilot-pending")
        with f.database.transaction() as c:
            c.execute("UPDATE pending_edit_requests SET primary_source_path=?,secondary_source_path=? WHERE id=?",
                      (str(f.session.source_path),str(secondary),pending))
        # Large random synthetic payload ensures the encrypted fixture must use multipart.
        media = f.settings.users_dir/"synthetic-multipart-padding.bin"
        with media.open("wb") as fd:
            for _ in range(65):
                fd.write(os.urandom(1024**2))
        phrase = secrets.token_hex(32)
        keyfile = work/"pass"
        keyfile.write_text(phrase,encoding="ascii")
        archive = BackupManager(f.settings.database_path,work/"archives",14)
        meta = archive.create_recovery_bundle(phrase,_standalone_sqlite=True)
        checkpoint = archive.create(phrase,_standalone_sqlite=True)
        # An independently restorable second state, not duplicate ciphertext
        # for the same manifest. Change only synthetic data.
        f.clock.advance(seconds=2)
        f.service.create_order(f.user_id,None,"pilot-second-state",account_purchase=True)
        second_meta = archive.create_recovery_bundle(phrase,_standalone_sqlite=True)
        second_db = archive.create(phrase,_standalone_sqlite=True)
        source = f.settings.database_path
        # Retain the path metadata for verification, but no active service or DB.
        yield (source, archive.backup_dir/meta["recovery_bundle_name"],
               archive.backup_dir/checkpoint["backup_name"], keyfile,
               archive.backup_dir/second_meta["recovery_bundle_name"],
               archive.backup_dir/second_db["backup_name"])
    finally:
        f.doCleanups()


def writer(bucket, profile, source, template, keyfile, work, barriers, out, db=False):
    try:
        work.mkdir()
        store = PilotStore(bucket,profile,checkpoints=db,barrier=barriers)
        phrase=keyfile.read_text(encoding="ascii")
        if db:
            checkpoint = work/"sqlite.enc"
            shutil.copyfile(template,checkpoint)
            publish_db(store,work,checkpoint,STAMP,lambda p:_prove(p,work/"decoded.sqlite3",phrase))
        else:
            digest_path = work/READBACK
            shutil.copyfile(template,digest_path)
            manager = BackupManager(source,work,14)
            promote(store,work,digest_path,
                    lambda digest:_restore(manager,READBACK,phrase,work/"restored",digest),
                    scheduled=STAMP.date().isoformat(),now=lambda:STAMP)
        out.put("PUBLISHED")
    except ApiError as err:
        out.put("CONTENDED" if err.code in ("409","412","Conflict","PreconditionFailed","OperationAborted")
                else "S3_"+err.code)
    except Exception as err:
        out.put(type(err).__name__)


def race(bucket, profile, source, template, keyfile, work, db=False):
    ctx = mp.get_context("spawn")
    barrier, out = ctx.Barrier(2), ctx.Queue()
    runners = [ctx.Process(target=writer,args=(bucket,profile,source,template,keyfile,
               work/f"writer-{db}-{i}",barrier,out,db)) for i in range(2)]
    try:
        for p in runners:
            p.start()
        results = [out.get(timeout=420) for _ in runners]
        if sorted(results) != ["CONTENDED","PUBLISHED"]:
            raise BackupError("Real S3 concurrent publication did not prove one winner: "+",".join(results))
    finally:
        for p in runners:
            if p.pid is not None:
                p.join(10)
                if p.is_alive():
                    p.terminate()
                    p.join(10)
        out.close()


def alert_roundtrip(work):
    key, cert = work/"tls.key",work/"tls.crt"
    subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                    "-keyout",str(key),"-out",str(cert),"-subj","/CN=localhost",
                    "-addext","subjectAltName=DNS:localhost"],check=True,capture_output=True,timeout=20)
    token = secrets.token_hex(32)
    events = []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self,*_args):
            pass

        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer "+token:
                self.send_error(403)
                return
            events.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1",0),Receiver) as server:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert,key)
        server.socket=context.wrap_socket(server.socket,server_side=True)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            (work/"notify.url").write_text(f"https://localhost:{server.server_port}/",encoding="ascii")
            (work/"notify.token").write_text(token,encoding="ascii")
            opener=build_opener(NoRedirect(),HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))))
            for state in ("ok","fail"):
                notify(state,"ravuna-ru-recovery.service",work,opener=opener)
        finally:
            server.shutdown()
            worker.join(5)
    if [e["state"] for e in events] != ["ok","fail"]:
        raise BackupError("Pilot HTTPS receiver did not acknowledge both outcomes")


def exercise(args, work):
    writer_store = PilotStore(args.bucket,args.writer_profile)
    reader = PilotStore(args.bucket,args.reader_profile)
    db_reader = PilotStore(args.bucket,args.reader_profile,checkpoints=True)
    common = ("--bucket",args.bucket)
    writer_store.ensure_bounded()
    for store in (reader, db_reader, PilotStore(args.bucket,args.db_writer_profile,checkpoints=True)):
        store.ensure_bounded()
    from contextlib import contextmanager
    with contextmanager(fixture)(work) as (source, full, db, keyfile, next_full, next_db):
        race(args.bucket,args.writer_profile,source,full,keyfile,work)
        race(args.bucket,args.db_writer_profile,source,db,keyfile,work,db=True)
        phrase=keyfile.read_text(encoding="ascii")
        seed=work/"second-point"
        seed.mkdir()
        next_archive=seed/READBACK
        shutil.copyfile(next_full,next_archive)
        manager=BackupManager(source,seed,14)
        next_stamp=STAMP + timedelta(days=1)
        promote(writer_store,seed,next_archive,
                lambda digest:_restore(manager,READBACK,phrase,seed/"restored",digest),
                scheduled=next_stamp.date().isoformat(),now=lambda:next_stamp)
        db_writer=PilotStore(args.bucket,args.db_writer_profile,checkpoints=True)
        publish_db(db_writer,seed,next_db,STAMP+timedelta(minutes=15),
                   lambda p:_prove(p,seed/"decoded.sqlite3",phrase))
        phase=work/"reader"
        phase.mkdir()
        index,etag=reader.get_catalog(phase)
        point=control(index,_validate_points)["points"][0]
        key=writer_store._key(point["slot"])
        body=phase/"body"
        body.write_bytes(b"synthetic overwrite probe")
        params=(*common,"--key",key,"--body",body)
        expect_denied(lambda:writer_store._api("put-object",*params),"AccessDenied","403")
        expect_denied(lambda:writer_store._api("put-object",*params,"--if-none-match","*"),
                      "PreconditionFailed","412")
        expect_denied(lambda:reader._api("delete-object",*common,"--key",key),"AccessDenied","403")
        expect_denied(lambda:db_writer._api("head-object",*common,"--key",key),"AccessDenied","403")
        expect_denied(lambda:writer_store._api("delete-bucket-policy",*common),"AccessDenied","403")
        expect_denied(lambda:writer_store.put_catalog(index,'"00000000000000000000000000000000"',phase),
                      "PreconditionFailed","412")
        margs=(*common,"--key",key)
        upload=writer_store._api("create-multipart-upload",*margs)["UploadId"]
        try:
            part=writer_store._api("upload-part",*margs,"--upload-id",upload,"--part-number","1","--body",body)
            parts=phase/"multipart.json"
            parts.write_text(json.dumps({"Parts":[{"PartNumber":1,"ETag":part["ETag"]}]}),encoding="ascii")
            expect_denied(lambda:writer_store._api("complete-multipart-upload",*margs,"--upload-id",upload,
                          "--multipart-upload","file://"+str(parts),"--if-none-match","*"),
                          "PreconditionFailed","412")
        finally:
            writer_store._api("abort-multipart-upload",*margs,"--upload-id",upload)
        if len(index["points"]) != 2:
            raise BackupError("Two distinct full points are required")
        from app.recovery_retention import regular_file_identity
        db_index,_=db_reader.get_catalog(phase)
        if len(db_index["points"]) != 2:
            raise BackupError("Two checkpoints are required")
        for i,full_point in enumerate(index["points"]):
            item=phase/f"full-{i}"
            item.mkdir()
            _read_point(reader,full_point,item/READBACK)
            report=_restore(BackupManager(source,item,14),READBACK,phrase,item/"restore",full_point["sha256"])
            if not report["distinct_media"]:
                raise BackupError("Synthetic media coverage missing")
        for i,db_point in enumerate(db_index["points"]):
            ciphertext=phase/f"db-{i}.enc"
            _read_point(db_reader,db_point,ciphertext,regular_file_identity)
            _prove(ciphertext,phase/f"db-{i}.sqlite3",phrase)
        result=restore_latest(db_reader,phase,phase/"db-only.sqlite3",phrase)
        if result["sqlite_only"]!="PASS":
            raise BackupError("Checkpoint did not restore")
        class Interrupted(BaseException):
            pass
        try:
            with reserve(writer_store,phase,_validate_points) as txn:
                pending_key=txn.key
                writer_store._api("create-multipart-upload",*common,"--key",writer_store._key(pending_key))
                raise Interrupted()
        except Interrupted:
            # The only two writers are joined above; no real worker points here.
            recover_interrupted_writer(writer_store,phase,_validate_points,pending_key)
        catalog,new_etag=reader.get_catalog(phase)
        if catalog!=index or not etag or not new_etag:
            raise BackupError("Interrupted operation changed the published point")
        alert_roundtrip(phase)
        print(json.dumps({"publication":"PASS","concurrent_full_and_db":"PASS","live_readback":"PASS",
                          "conditional_and_iam":"PASS","full_points_restored":2,"checkpoints_restored":2,
                          "full_media_references":report["media_references"],
                          "interrupted_upload_cleanup":"PASS","local_https_alert_delivery":"PASS"}),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode",choices=("plan","apply","cleanup"),default="plan",nargs="?")
    for name in ("bucket","run-id","admin-profile","writer-profile","db-writer-profile","reader-profile",
                 "writer-id","db-writer-id","reader-id"):
        p.add_argument("--"+name)
    p.add_argument("--authorize-synthetic-cloud-actions",action="store_true")
    args=p.parse_args()
    if args.mode=="plan":
        print(json.dumps({"mode":"plan","endpoint":ENDPOINT,"storage_class":"STANDARD",
                          "budget_rub_with_unused_free_tier":0,"purchased_services":False,
                          "media":"generated only","bucket_template":"ravuna-pilot-<32 lowercase hex>"}))
        return 0
    try:
        if (not args.authorize_synthetic_cloud_actions or not args.run_id
                or not re.fullmatch(r"[a-f0-9]{32}",args.run_id)
                or args.bucket!="ravuna-pilot-"+args.run_id or not args.admin_profile):
            raise BackupError("Explicit authorization and a unique pilot bucket are required")
        admin=PilotStore(args.bucket,args.admin_profile)
        if args.mode=="cleanup":
            cleanup(admin,args.run_id)
            print('{"teardown":"PASS"}')
            return 0
        profiles=[args.admin_profile,args.writer_profile,args.db_writer_profile,args.reader_profile]
        ids=[args.writer_id,args.db_writer_id,args.reader_id]
        if (not all(x and re.fullmatch(r"[A-Za-z0-9_-]{2,64}",x) for x in profiles+ids)
                or len(set(profiles))!=4 or len(set(ids))!=3):
            raise BackupError("Use distinct bootstrap, writer, DB writer and reader identities")
        common=("--bucket",args.bucket)
        with tempfile.TemporaryDirectory(prefix="ravuna-s3-synthetic-") as tmp:
            work=Path(tmp)
            expect_denied(lambda:admin._api("head-bucket",*common),"404","NotFound","NoSuchBucket")
            admin._api("create-bucket",*common,"--acl","private")
            put_json(admin,"put-bucket-tagging","--tagging",
                     {"TagSet":[{"Key":"RavunaRecoveryPilot","Value":args.run_id}]},work)
            try:
                put_json(admin,"put-bucket-lifecycle-configuration","--lifecycle-configuration",
                         {"Rules":[{"ID":"abort-synthetic","Status":"Enabled","Filter":{"Prefix":"ravuna/"},
                                    "AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]},work)
                put_json(admin,"put-bucket-policy","--policy",bucket_policy(args.bucket,*ids),work)
                # A distinct reader will test that inherited roles do
                # not grant wider destructive actions than the bucket policy.
                exercise(args,work)
            finally:
                cleanup(admin,args.run_id)
                print('{"teardown":"PASS"}',flush=True)
        return 0
    except Exception as err:
        print(json.dumps({"status":"FAIL","error_class":type(err).__name__,
                          "s3_code":err.code if isinstance(err,ApiError) else None}))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
