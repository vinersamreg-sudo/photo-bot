"""Three delivered results, conservative obligations, file-by-file private cleanup.

Dry-run uses mode=ro/query_only and never creates a Database or writes a report.
Execution re-plans under the media writer guard and BEGIN IMMEDIATE. Filesystem
unlink is not transactional: interruption rolls DB changes back, and a retry
reconciles the same results (which may still be visible; missing files are not
counted as newly deleted).
Attempts, payment/commerce/referral evidence and feedback are never deleted.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import hashlib
import re
import stat
import sqlite3

from app.database import Database, ReadOnlyDatabase
from app.media_guard import media_guard

LIMIT = 3
FIRST_BATCH_LIMIT = 10
PATHS = {
    "gallery_versions": ("source_path", "secondary_source_path", "preview_watermarked_path", "original_path"),
    "generation_attempts": ("source_path", "secondary_source_path", "original_result_path", "demo_result_path"),
    "gallery_items": ("original_source_path", "secondary_source_path", "cover_preview_path", "preview_small_path", "preview_large_path"),
    "demo_sessions": ("source_file_path", "secondary_source_file_path"),
    "pending_edit_requests": ("primary_source_path", "secondary_source_path"),
}


def successful_results(connection, user_id=None):
    """Rank before checking file availability; never promote a retired result.

Succeeded is the existing delivery/debit commit contract; legacy gallery-only
rows retain their explicit succeeded status. Opening/favouriting is not a clock.
    """
    return connection.execute("""SELECT v.*,i.user_id,i.deleted,i.retention_until,i.purge_after,
        i.created_at AS work_created_at,
        COALESCE(a.completed_at,v.created_at) AS completed_at
        FROM gallery_versions v JOIN gallery_items i ON i.id=v.gallery_item_id
        LEFT JOIN generation_attempts a ON a.id=v.attempt_id
        WHERE v.status='succeeded' AND (v.attempt_id IS NULL OR a.status='succeeded')
          AND (? IS NULL OR i.user_id=?)
        ORDER BY julianday(COALESCE(a.completed_at,v.created_at)) DESC,v.id DESC""",
        (user_id,user_id)).fetchall()


def result_keys(connection,user_id=None):
    # Attempts outlive gallery rows. Their chronology prevents an older paid
    # result resurfacing after newer expired gallery rows have been deleted.
    return connection.execute("""SELECT * FROM (
        SELECT a.user_id,'attempt:'||a.id AS result_key,v.id AS version_id,
               COALESCE(a.completed_at,v.created_at,a.created_at) AS completed_at
        FROM generation_attempts a LEFT JOIN gallery_versions v ON v.attempt_id=a.id
        WHERE a.status='succeeded' AND (? IS NULL OR a.user_id=?)
        UNION ALL
        SELECT i.user_id,'legacy:'||v.id,v.id,v.created_at
        FROM gallery_versions v JOIN gallery_items i ON i.id=v.gallery_item_id
        WHERE v.attempt_id IS NULL AND v.status='succeeded' AND (? IS NULL OR i.user_id=?)
        ) ORDER BY julianday(completed_at) DESC,result_key DESC""",(user_id,user_id,user_id,user_id)).fetchall()


def visible_results(connection, user_id):
    # Deleted results still occupy their chronological slot: no history revival.
    if not user_id:return []
    rows={r['id']:r for r in successful_results(connection,user_id)}
    return [rows[k['version_id']] for k in result_keys(connection,user_id)[:LIMIT]
            if k['version_id'] in rows and not rows[k['version_id']]['deleted']]


def _time(value):
    try:
        d = datetime.fromisoformat(value)
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (TypeError, ValueError):
        return None


def _path(settings, value):
    p = Path(value)
    return Path(os.path.abspath(p if p.is_absolute() else settings.base_dir/p))


def _legacy_review(path, now):
    """Explicit private operator evidence, not inferred consent or a C2 opt-in.

    Hash references attest to a separately verified historical legal review.
    Syntax/hash strings cannot prove its legal truth; missing evidence is NOT
    permission. Exact work/result timestamps bind each decision to its target.
    The <=10-entry set also prevents a retry from spilling into the next batch.
    """
    if path is None:
        return {}, None
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Unsafe legacy review')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 65536 or
                (os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid != os.geteuid()))):
            raise ValueError('Unsafe legacy review')
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('Oversized legacy review')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid legacy review')
    reviewed = _time(value.get('reviewed_at'))
    entries = value.get('results')
    if (value.get('schema_version') != 1 or value.get('basis') != 'legacy_expiry_30d' or
            not reviewed or reviewed > now or not isinstance(entries, list) or
            not 1 <= len(entries) <= FIRST_BATCH_LIMIT):
        raise ValueError('Invalid legacy review')
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Invalid legacy review target')
        identifier = entry.get('version_id')
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 128 or
                identifier in result or not _time(entry.get('work_created_at')) or
                not _time(entry.get('completed_at')) or
                any(not isinstance(entry.get(key), str) or not entry[key] or len(entry[key]) > 128
                    for key in ('user_id', 'gallery_item_id')) or
                any(not isinstance(entry.get(key), str) or
                    not re.fullmatch(r'[0-9a-f]{64}', entry[key])
                    for key in ('terms_sha256', 'evidence_sha256'))):
            raise ValueError('Invalid legacy review target')
        result[identifier] = entry
    return result, hashlib.sha256(raw).hexdigest()


def _safe_metadata(path, root):
    """Validate lexical containment and every component, without resolve/follow."""
    root = Path(os.path.abspath(root))
    relative = path.relative_to(root)
    if not relative.parts or '..' in relative.parts:
        raise ValueError("Unsafe cleanup path")
    if any(p.is_symlink() for p in root.parents):
        raise ValueError("Unsafe cleanup root")
    for parent in (root, *[root.joinpath(*relative.parts[:n]) for n in range(1,len(relative.parts))]):
        if parent.is_symlink():
            raise ValueError("Unsafe cleanup parent")
        if not parent.exists():return None
        if not parent.is_dir():raise ValueError("Unsafe cleanup parent")
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ValueError("Unsafe cleanup file")
    return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)


def _unlink(path, root, identity):
    if _safe_metadata(path,root) != identity:
        raise RuntimeError("Cleanup file changed")
    if os.name != 'posix':
        # Production deletion requires dir_fd/O_NOFOLLOW, not a Windows fallback.
        raise RuntimeError("Secure deletion requires POSIX dir_fd support")
    parts = path.relative_to(root).parts
    # Walk the root's ancestors too: O_NOFOLLOW on only the final root would
    # still follow an intermediate symlink swapped after metadata validation.
    fd = os.open(root.anchor,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for name in (*root.parts[1:], *parts[:-1]):
            child = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        st=os.stat(parts[-1],dir_fd=fd,follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)!=identity:
            raise RuntimeError("Cleanup file changed")
        os.unlink(parts[-1],dir_fd=fd)
    finally:
        os.close(fd)


class ResultRetention:
    def __init__(self, settings, database=None, clock=None):
        self.settings=settings
        self.database=database
        self.clock=clock or (lambda: datetime.now(timezone.utc))

    def _plan(self,c,user_id=None,*,batch_limit=None,first_batch=False,review_path=None):
        now=self.clock();rows=successful_results(c,user_id);keys=result_keys(c,user_id);counts=Counter();latest=set()
        reviews, review_digest = _legacy_review(review_path, now) if first_batch else ({}, None)
        policy_counts = Counter()
        # First-batch scope deliberately excludes all commerce history, including
        # pending/late orders, old fully spent purchases and promotional originals.
        commerce_users = set()
        if first_batch:
            batch_limit = FIRST_BATCH_LIMIT
            commerce_users = {r[0] for r in c.execute('''SELECT user_id FROM payment_orders
                UNION SELECT user_id FROM payment_intents
                UNION SELECT user_id FROM continuation_pack_grants
                UNION SELECT user_id FROM unlock_entitlements''')}
        for r in keys:
            counts[r['user_id']]+=1
            if counts[r['user_id']]<=LIMIT:latest.add(r['version_id'])
        protected=Counter();targets=[]
        paid={};unknown_paid_users=set()
        for r in c.execute("SELECT user_id,paid_at,created_at FROM payment_orders WHERE status IN ('paid','delivered','delivery_pending','partially_refunded','refund_pending')"):
            t=_time(r['paid_at']) or _time(r['created_at'])
            if t:paid[r['user_id']]=max(paid.get(r['user_id'],t),t)
            else:unknown_paid_users.add(r['user_id'])
        busy=c.execute("""SELECT (SELECT COUNT(*) FROM generation_attempts WHERE status IN ('pending','processing'))+
            (SELECT COUNT(*) FROM generation_credit_reservations WHERE status='reserved')+
            (SELECT COUNT(*) FROM unlock_entitlements WHERE status='reserved')+
            (SELECT COUNT(*) FROM max_dialogs WHERE state='processing')""").fetchone()[0]
        selected={r[0] for r in c.execute("""SELECT current_version_id FROM max_dialogs
            WHERE current_version_id IS NOT NULL AND state IN
            ('processing','waiting_for_correction','confirmation','waiting_for_prompt','waiting_for_source')""")}
        editing_items={r[0] for r in c.execute("""SELECT current_gallery_item_id FROM max_dialogs
            WHERE current_gallery_item_id IS NOT NULL AND state IN
            ('processing','waiting_for_correction','confirmation','waiting_for_prompt','waiting_for_source')""")}
        pending_items={r[0] for r in c.execute("SELECT gallery_item_id FROM pending_edit_requests WHERE status NOT IN ('completed','cancelled','expired','failed')")}
        context_items={r[0] for r in c.execute("""SELECT gallery_item_id FROM provider_contexts
            WHERE status='active' AND julianday(expires_at)>julianday(?)""",(now.isoformat(),))}
        orders=defaultdict(list)
        for r in c.execute("""SELECT COALESCE(o.version_id,i.version_id) version_id,o.status,o.payment_purpose
            FROM payment_orders o LEFT JOIN payment_intents i ON i.id=o.intent_id"""):
            if r['version_id']:orders[r['version_id']].append(r)
        legacy_pending={r[0] for r in c.execute("""SELECT version_id FROM payment_intents WHERE version_id IS NOT NULL
            AND status IN ('pending','paid') AND NOT EXISTS(SELECT 1 FROM payment_orders o WHERE o.intent_id=payment_intents.id)
            AND (status='pending' OR NOT EXISTS(SELECT 1 FROM gallery_versions v WHERE v.id=version_id AND v.delivery_count>0))""")}
        retired=0
        for r in rows:
            if not any(r[k] for k in PATHS['gallery_versions']):
                retired+=1;continue
            expired=(_time(r['retention_until']) or now+timedelta(days=1))<=now
            if r['id'] in latest and not expired:continue
            reason=None
            complete=_time(r['completed_at'])
            paid_anchor=paid.get(r['user_id'])
            unlocked_anchor=_time(r['last_delivered_at']) or _time(r['unlocked_at'])
            anchor=max([t for t in (complete,paid_anchor,unlocked_anchor) if t],default=None)
            if not complete:reason='completion_time_unverified'
            elif r['user_id'] in unknown_paid_users:reason='payment_time_unverified'
            elif r['unlock_status']=='unlocked' and not unlocked_anchor:reason='original_retention_unverified'
            elif busy:reason='in_flight_operations'
            elif r['deleted'] and (_time(r['purge_after']) or now+timedelta(days=1))>now:
                reason='trash_retention'
            elif r['id'] in selected or r['gallery_item_id'] in editing_items:reason='editing_dialog'
            elif r['gallery_item_id'] in pending_items:reason='pending_edit'
            elif r['gallery_item_id'] in context_items:reason='provider_context_in_use'
            elif r['id'] in legacy_pending:reason='legacy_payment_obligation'
            elif r['unlock_status']=='unlocked' and not r['delivery_count']:
                reason='original_delivery_unconfirmed'
            elif any(o['status'] not in ('paid','delivered','partially_refunded','refunded') or
                     (o['payment_purpose']=='original_download' and o['status']!='delivered') for o in orders[r['id']]):
                reason='payment_or_delivery_obligation'
            elif (paid_anchor or unlocked_anchor or r['unlock_status']=='unlocked') and (
                    not anchor or now < anchor+timedelta(days=self.settings.paid_retention_days)):
                reason='paid_retention'
            if not reason and first_batch:
                created = _time(r['work_created_at'])
                if not created or not _time(r['retention_until']):
                    reason = 'legacy_expiry_unverified'
                elif (not expired or now < created + timedelta(days=30) or
                      now < complete + timedelta(days=30)):
                    reason = 'early_eviction_excluded'
                else:
                    policy_counts['legacy_expired_results'] += 1
                    review = reviews.get(r['id'])
                    if r['user_id'] in commerce_users or r['unlock_status'] != 'demo':
                        reason = 'commerce_history_first_batch'
                    elif review is None:
                        reason = 'legacy_terms_unverified'
                    elif (review['user_id'] != r['user_id'] or
                          review['gallery_item_id'] != r['gallery_item_id'] or
                          _time(review['work_created_at']) != created or
                          _time(review['completed_at']) != complete):
                        reason = 'legacy_review_target_mismatch'
            if reason:protected[reason]+=1
            else:targets.append(r)
        eligible_total=len(targets)
        if batch_limit:
            targets=sorted(targets,key=lambda r:(_time(r['completed_at']) or now,r['id']))[:batch_limit]
        ids={r['id'] for r in targets};attempts={r['attempt_id'] for r in targets if r['attempt_id']}
        items={r['gallery_item_id'] for r in targets}
        dead_items={i for i in items if not c.execute('SELECT 1 FROM gallery_versions WHERE gallery_item_id=? AND id NOT IN ('+','.join('?' for _ in ids)+") AND (source_path<>'' OR secondary_source_path IS NOT NULL OR preview_watermarked_path IS NOT NULL OR original_path IS NOT NULL) LIMIT 1",(i,*sorted(ids))).fetchone()}
        dead_sessions={r[0] for i in dead_items for r in c.execute('SELECT id FROM demo_sessions WHERE gallery_item_id=?',(i,))}
        dead_requests={r[0] for i in dead_items for r in c.execute(
            "SELECT id FROM pending_edit_requests WHERE gallery_item_id=? AND status IN ('completed','cancelled','expired','failed')",(i,))}
        skipped={'gallery_versions':ids,'generation_attempts':attempts,'gallery_items':dead_items,
                 'demo_sessions':dead_sessions,'pending_edit_requests':dead_requests}
        candidates=set();references=set();owners=defaultdict(set);unsafe_candidates=0
        for table,columns in PATHS.items():
            owner="(SELECT user_id FROM gallery_items WHERE id=gallery_item_id)" if table=='gallery_versions' else 'user_id'
            for r in c.execute('SELECT id,'+owner+' AS owner,'+','.join(columns)+' FROM '+table):
                destination=candidates if r['id'] in skipped.get(table,set()) else references
                for column in columns:
                    if r[column]:
                        path=_path(self.settings,r[column]);destination.add(path)
                        if destination is candidates:
                            owners[path].add(r['owner'])
                            if '..' in Path(r[column]).parts:unsafe_candidates+=1
        shared=candidates & references;candidates-=references
        files=[];anomalies=unsafe_candidates;missing=0
        root=Path(os.path.abspath(self.settings.users_dir))
        for path in sorted(candidates):
            try:
                if path.relative_to(root).parts[0] not in owners[path]:
                    raise ValueError('Foreign private file')
                identity=_safe_metadata(path,root)
                if identity is None:missing+=1
                else:files.append((path,root,identity))
            except (OSError,ValueError):anomalies+=1
        # Cache files are derived, not customer originals. A grace period avoids
        # upload-in-progress files; the media writer guard excludes active reads.
        cache=self.settings.temp_dir/'max-contact-sheets'
        if not first_batch and cache.is_symlink():anomalies+=1
        elif not first_batch and cache.is_dir() and user_id is None:
            cached=sorted(cache.glob('*.jpg'));cache_selected=0
            for path in cached:
                try:
                    identity=_safe_metadata(path,cache)
                    if identity and path.stat().st_mtime < now.timestamp()-self.settings.cleanup_temp_retention_hours*3600:
                        if not batch_limit or cache_selected<200:
                            files.append((path,cache,identity));cache_selected+=1
                except (OSError,ValueError):anomalies+=1
        deletable=sum(bool(r['attempt_id']) and not self._referenced(c,'gallery_versions',r['id']) for r in targets)
        report={'mode':'dry-run','completed_at':now.isoformat(),'limit':LIMIT,'successful_results':len(keys),
                'gallery_results_present':len(rows),'retired_metadata_results':retired,
                'over_limit_results':sum(max(0,n-LIMIT) for n in counts.values()),
                'eligible_results_total':eligible_total,'batch_limit':batch_limit,
                'candidate_results':len(ids),'planned_version_deletes':deletable,
                'planned_metadata_stubs':len(ids)-deletable,'protected_results':sum(protected.values()),
                'protection_reasons':dict(sorted(protected.items())),
                'candidate_files':len(files),'candidate_bytes':sum(x[2][2] for x in files),
                'shared_files_preserved':len(shared),'already_missing_files':missing,
                'path_anomaly_count':anomalies,'busy_operations':busy,
                'removed_files':0,'removed_bytes':0,'removed_versions':0,'removed_items':0,
                'metadata_stubs_preserved':0,'database_access':'mode=ro; query_only=ON'}
        if first_batch:
            report.update(profile='first-batch-legacy-expiry', review_sha256=review_digest,
                          reviewed_targets=len(reviews), **policy_counts)
            report.setdefault('legacy_expired_results', 0)
        return report,targets,attempts,dead_items,dead_sessions,files

    @staticmethod
    def _referenced(c,table,identifier):
        # Keep financial targets, feedback and live state; never cascade these.
        for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            other=row[0]
            for fk in c.execute('PRAGMA foreign_key_list('+other+')'):
                if fk['table']==table and other!='gallery_versions' and c.execute(
                    'SELECT 1 FROM '+other+' WHERE '+fk['from']+'=? LIMIT 1',(identifier,)).fetchone():return True
        for other in ('payment_orders','payment_intents'):
            if table=='gallery_versions' and c.execute('SELECT 1 FROM '+other+' WHERE version_id=? LIMIT 1',(identifier,)).fetchone():return True
        return False

    def _apply(self,c,user_id,*,first_batch=False,review_path=None,expected_review_digest=None):
        report,targets,attempts,items,sessions,files=self._plan(
            c,user_id,batch_limit=100,first_batch=first_batch,review_path=review_path)
        report.update(mode='execute',database_access='BEGIN IMMEDIATE; revalidated plan')
        if first_batch and report['review_sha256'] != expected_review_digest:
            raise RuntimeError('Legacy review changed before execution')
        if report['busy_operations']:
            report['mode']='busy'
            return report
        if report['path_anomaly_count']:raise RuntimeError('Result cleanup refused: unsafe path')
        for path,root,identity in files:
            _unlink(path,root,identity)
            report['removed_files']+=1;report['removed_bytes']+=identity[2]
        for r in targets:
            c.execute("UPDATE gallery_versions SET source_path='',secondary_source_path=NULL,preview_watermarked_path=NULL,original_path=NULL WHERE id=?",(r['id'],))
            if r['attempt_id'] and not self._referenced(c,'gallery_versions',r['id']):
                c.execute('DELETE FROM gallery_versions WHERE id=?',(r['id'],));report['removed_versions']+=1
            else:report['metadata_stubs_preserved']+=1
        for attempt in sorted(attempts):
            c.execute("UPDATE generation_attempts SET source_path='',secondary_source_path=NULL,original_result_path=NULL,demo_result_path=NULL WHERE id=?",(attempt,))
        for session in sorted(sessions):
            c.execute("UPDATE demo_sessions SET source_file_path='',secondary_source_file_path=NULL WHERE id=?",(session,))
        for item in sorted(items):
            c.execute("""UPDATE pending_edit_requests SET primary_source_path=NULL,secondary_source_path=NULL
                WHERE gallery_item_id=? AND status IN ('completed','cancelled','expired','failed')""",(item,))
            c.execute("UPDATE gallery_items SET original_source_path='',secondary_source_path=NULL,cover_preview_path=NULL,preview_small_path=NULL,preview_large_path=NULL,current_best_version_id=NULL WHERE id=?",(item,))
            if not self._referenced(c,'gallery_items',item) and not c.execute('SELECT 1 FROM gallery_versions WHERE gallery_item_id=?',(item,)).fetchone():
                c.execute('DELETE FROM gallery_items WHERE id=?',(item,));report['removed_items']+=1
        # A mixed-version work may have selected an old version explicitly.
        for r in targets:
            c.execute('UPDATE gallery_items SET current_best_version_id=NULL WHERE current_best_version_id=?',(r['id'],))
        for user in sorted({r['user_id'] for r in targets}):
            c.execute('DELETE FROM tags WHERE user_id=? AND NOT EXISTS(SELECT 1 FROM gallery_item_tags t WHERE t.tag_id=tags.id)',(user,))
        if c.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('Result cleanup integrity check failed')
        return report

    def run(self,*,execute=False,user_id=None,first_batch=False,review_path=None):
        review_digest = None
        if review_path is not None and not first_batch:
            raise ValueError('Legacy review requires first-batch profile')
        if execute and first_batch:
            if self.settings.result_retention_enabled:
                raise ValueError('Automatic result retention must remain OFF')
            reviews, review_digest = _legacy_review(review_path, self.clock())
            if not reviews:
                raise ValueError('First batch requires verified legacy review')
        if not execute:
            with ReadOnlyDatabase(self.settings.database_path).read() as c:
                c.execute('BEGIN')
                report,*_=self._plan(c,user_id,first_batch=first_batch,review_path=review_path)
                report['sqlite_free_pages']=c.execute('PRAGMA freelist_count').fetchone()[0]
                report['sqlite_page_size']=c.execute('PRAGMA page_size').fetchone()[0]
                return report
        with media_guard(self.settings.data_dir,exclusive=True,blocking=False) as acquired:
            if not acquired:return {'mode':'busy','removed_files':0,'removed_bytes':0}
            database=self.database or Database(self.settings.database_path)
            with database.transaction() as c:report=self._apply(
                c,user_id,first_batch=first_batch,review_path=review_path,
                expected_review_digest=review_digest)
            # No user identifiers or private file names in the operator receipt.
            from app.storage import PrivateStorage
            PrivateStorage.write_private(self.settings.data_dir/'result_retention_last.json',json.dumps(report,sort_keys=True).encode())
            return report


def main():
    from app.config import load_settings
    parser=argparse.ArgumentParser(description='Read-only three-result retention plan; explicit --execute required.')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--first-batch',action='store_true',help='At most 10 reviewed legacy-expired results; no cache sweep')
    parser.add_argument('--legacy-review',type=Path,help='Private verified historical-terms evidence (not consent)')
    args=parser.parse_args()
    if args.execute and not args.first_batch:
        parser.error('Operator execution requires --first-batch; automatic cleanup is separately gated')
    try:
        report=ResultRetention(load_settings()).run(execute=args.execute,first_batch=args.first_batch,
                                                   review_path=args.legacy_review)
    except (OSError,ValueError,RuntimeError,sqlite3.Error):
        print(json.dumps({'status':'FAIL','reason':'result_retention_safety_gate'}));return 2
    print(json.dumps(report,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
