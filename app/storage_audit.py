"""Read-only allocated-byte inventory; no private names, prompts or file contents."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import sqlite3

from app.database import ReadOnlyDatabase
from app.result_retention import PATHS, ResultRetention, _path
from app.recovery_retention import RetentionError, plan


def _walk(root, counts):
    """Never follow a symlink, including ancestors of the requested root."""
    if any(p.is_symlink() for p in (root, *root.parents)):
        counts['unsafe_entries'] += 1
        return
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    # Windows DirEntry.stat can report zero inode identifiers;
                    # path lstat obtains the real identity for hardlink dedup.
                    info = Path(entry.path).lstat()
                    if stat.S_ISDIR(info.st_mode):
                        yield from _walk(Path(entry.path), counts)
                    elif stat.S_ISREG(info.st_mode):
                        yield Path(entry.path), info
                    else:
                        counts['unsafe_entries'] += 1
                except OSError:
                    counts['unreadable_entries'] += 1
    except FileNotFoundError:
        counts['missing_roots'] += 1
    except OSError:
        counts['unreadable_entries'] += 1


def audit(settings, confirmed_backup=None, review_path=None):
    counts = Counter()
    refs = defaultdict(set)
    with ReadOnlyDatabase(settings.database_path).read() as connection:
        connection.execute('BEGIN')
        for table, columns in PATHS.items():
            for row in connection.execute('SELECT ' + ','.join(columns) + ' FROM ' + table):
                for column, value in zip(columns, row):
                    if value:
                        category = ('previews' if any(x in column for x in ('preview', 'demo_result'))
                                    else 'generated_versions' if column in ('original_path', 'original_result_path')
                                    else 'source_inputs')
                        refs[_path(settings, value)].add(category)
    categories = defaultdict(lambda: {'files': 0, 'logical_bytes': 0, 'allocated_bytes': 0})
    seen = set()
    def add(path, info, category):
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            counts['duplicate_inode_entries'] += 1
            return
        seen.add(identity)
        item = categories[category]
        item['files'] += 1
        item['logical_bytes'] += info.st_size
        item['allocated_bytes'] += getattr(info, 'st_blocks', (info.st_size + 511) // 512) * 512
    for path, info in _walk(settings.users_dir, counts):
        roles = refs.get(path, set())
        add(path, info, next(iter(roles)) if len(roles) == 1 else
            'shared_media' if roles else 'unclassified_private_files')
    for root, category in ((settings.logs_dir, 'logs'), (settings.temp_dir, 'caches_and_temp'),
                           (settings.backup_dir_path, 'backups')):
        for path, info in _walk(root, counts):
            kind = category
            if category == 'backups':
                kind = ('recovery_snapshots' if path.name.startswith('ravuna-recovery-') and path.name.endswith('.tar.gz.enc')
                        else 'sqlite_snapshots' if path.name.startswith('pixora-') and path.name.endswith('.sqlite3.enc')
                        else 'backup_metadata_and_other')
            add(path, info, kind)
    # Database sidecars are not cache; never infer that a WAL can be removed.
    for suffix in ('', '-wal', '-shm', '-journal'):
        path = Path(str(settings.database_path) + suffix)
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and not any(p.is_symlink() for p in path.parents):
                add(path, info, 'sqlite_and_sidecars')
            else:
                counts['unsafe_entries'] += 1
        except FileNotFoundError:
            pass
    usage = shutil.disk_usage(settings.base_dir)
    retention = ResultRetention(settings).run(first_batch=True, review_path=review_path)
    technical = ResultRetention(settings).run()
    recovery = {'status': 'NOT MEASURABLE', 'reason': 'confirmed_newest_backup_required'}
    if confirmed_backup:
        try:
            seven = plan(settings.backup_dir_path, confirmed_backup=confirmed_backup, keep=7)
            three = plan(settings.backup_dir_path, confirmed_backup=confirmed_backup, keep=3)
            extra = {entry['backup_name'] for entry in three['entries'] if entry['action'] == 'DELETE'} - {
                entry['backup_name'] for entry in seven['entries'] if entry['action'] == 'DELETE'}
            allocated_extra = 0
            for name in extra:
                info = (settings.backup_dir_path / name).lstat()
                if info.st_nlink == 1:
                    allocated_extra += getattr(info, 'st_blocks', (info.st_size + 511) // 512) * 512
            # plan is only a read-only hypothetical count comparison; production
            # application of keep<7 additionally requires fresh off-site proof.
            recovery = {'status': 'HYPOTHETICAL_NOT_AUTHORIZATION', 'successful': seven['successful'],
                        'keep7_delete_logical_bytes': seven['delete_bytes'],
                        'keep3_delete_logical_bytes': three['delete_bytes'],
                        'incremental_7_to_3_logical_bytes': three['delete_bytes'] - seven['delete_bytes'],
                        'incremental_7_to_3_allocated_bytes_single_link_only': allocated_extra,
                        'offsite_recheck_required': True}
        except (RetentionError, OSError, ValueError, TypeError):
            recovery = {'status': 'BLOCKED', 'reason': 'unproven_or_unsafe'}
    return {'mode': 'read-only', 'database_access': 'mode=ro; query_only=ON',
            'cutoff_utc': datetime.now(timezone.utc).isoformat(),
            'filesystem': {'total_bytes': usage.total, 'used_bytes': usage.used, 'free_bytes': usage.free},
            'categories': dict(sorted(categories.items())), 'scan_findings': dict(counts),
            'inventoried_allocated_bytes': sum(x['allocated_bytes'] for x in categories.values()),
            'scope': 'application mutable storage only; filesystem totals include other services',
            'allocation_measurement': 'POSIX st_blocks; rounded estimate where st_blocks is unavailable',
            'free_photo_cleanup': retention,
            'technical_media_plan_not_legal_permission': technical,
            'protected_data': {'results': technical['protected_results'],
                               'reasons': technical['protection_reasons'],
                               'paid_data_deletion_authorized': False},
            'recovery_comparison': recovery,
            'warning': 'Bytes are not summed across plans; no cache/log/SQLite deletion authorized. Live scan is non-atomic.'}


def main(argv=None):
    from app.config import load_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirmed-backup', help='Newest locally proved recovery basename; no path')
    parser.add_argument('--legacy-review', type=Path, help='Optional private cohort evidence; never authorizes execution')
    args = parser.parse_args(argv)
    try:
        report = audit(load_settings(), args.confirmed_backup, args.legacy_review)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print(json.dumps({'status': 'BLOCKED', 'reason': 'storage_audit_read_failed'}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
