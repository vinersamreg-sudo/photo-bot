"""Offline schema-16 -> 17 rehearsal. Use only a verified, isolated backup copy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as persistence
from app.config import Settings
from app.result_retention import PATHS, ResultRetention
from scripts.check_migration_copy import _fingerprint_rows, _read_only_connection, verify_migration_copy

SCHEMA17 = {"gallery_retention_terms", "payment_retention_terms", "retention_holds"}


def snapshot(db, *, before=False):
    with closing(_read_only_connection(db)) as c:
        version = persistence.schema_version(c)
        names = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        schema = dict(c.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or c.execute(
            "PRAGMA foreign_key_check"
        ).fetchone() is not None:
            raise ValueError("SQLite checks failed")
        if before and (version != 16 or SCHEMA17 & names):
            raise ValueError("Expected clean schema 16")
        # Sort typed, encoded fingerprints deterministically by stable rowid.
        # No customer values or hashes are returned by the CLI.
        rows = {}
        for name in names - SCHEMA17:
            quoted = '"' + name.replace('"', '""') + '"'
            query = ("SELECT * FROM schema_migrations WHERE version<=16 ORDER BY version"
                     if name == "schema_migrations" else f"SELECT * FROM {quoted} ORDER BY rowid")
            rows[name] = _fingerprint_rows(c.execute(query))
        terms = {table: c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                 for table in SCHEMA17 & names}
        return version, rows, terms, schema


def _sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def media_inventory(db, source_users, local_users):
    paths = set()
    with closing(_read_only_connection(db)) as c:
        for table, columns in PATHS.items():
            for row in c.execute(f"SELECT {','.join(columns)} FROM {table}"):
                for raw in filter(None, row):
                    rel = Path(raw).relative_to(source_users)
                    if not rel.parts or ".." in rel.parts:
                        raise ValueError("Unsafe media reference")
                    paths.add(rel)
    files = {}
    for rel in paths:
        candidate = local_users / rel
        if not candidate.is_file() or candidate.is_symlink() or not candidate.resolve().is_relative_to(local_users):
            raise ValueError("Missing or unsafe media reference")
        files[rel] = _sha(candidate)
    return files


def _crash(db, stage):
    class CrashConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if stage == "before-commit" and "VALUES(17,?,?)" in sql:
                super().execute(sql, *args)
                os._exit(73)
            return super().execute(sql, *args)

        def commit(self):
            result = super().commit()
            if stage == "after-commit" and persistence.schema_version(self) == 17:
                os._exit(73)
            return result

    class CrashDatabase(persistence.Database):
        def connect(self):
            c = sqlite3.connect(self.path, isolation_level=None, factory=CrashConnection)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            return c

    CrashDatabase(db)
    return 2


def rehearse(source, scratch, source_users, local_users):
    # The backup must contain committed pages in its main file; an existing
    # live or concurrently written database is not a rehearsal input.
    if any(source.with_name(source.name+s).exists() for s in ("-wal","-shm","-journal")):
        raise ValueError("Use a standalone restored database")
    original = snapshot(source, before=True)
    before_sha = _sha(source)
    media = media_inventory(source, source_users, local_users)
    with tempfile.TemporaryDirectory(prefix="retention-rehearsal-", dir=scratch) as d:
        root = Path(d)
        (root / "data").mkdir()
        candidate = root / "data/photo_bot.sqlite3"
        result = verify_migration_copy(source, candidate)
        after = snapshot(candidate)
        if (after[0] != 17 or after[1] != original[1] or set(after[2]) != SCHEMA17
                or any(after[2].values())
                or any(after[3].get(k) != v for k, v in original[3].items())):
            raise ValueError("Migration changed existing rows, schema or historic policy")
        persistence.Database(candidate)
        if snapshot(candidate) != after:
            raise ValueError("Migration replay changed data")
        settings = Settings("", "fake", "test", root)
        before_attempt = _sha(candidate)
        try:
            ResultRetention(settings).run(execute=True)
        except RuntimeError as e:
            if "not enabled" not in str(e):
                raise
        else:
            raise ValueError("Disabled cleanup did not refuse execution")
        if _sha(candidate) != before_attempt or (root / "data/result-retention.lock").exists():
            raise ValueError("Disabled cleanup mutated the rehearsal")
        for stage, want in (("before-commit", 16), ("after-commit", 17)):
            target = root / (stage + ".sqlite3")
            with closing(_read_only_connection(source)) as src, closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
            proc = subprocess.run([sys.executable, "-B", __file__, "--internal-crash", stage,
                                   str(target)], capture_output=True, timeout=30)
            # SQLite may need write access to roll back a hot rollback journal.
            # This is only the disposable fault-injection copy.
            with closing(sqlite3.connect(target)) as c:
                c.execute("SELECT 1 FROM schema_migrations").fetchone()
            if proc.returncode != 73 or snapshot(target)[0] != want:
                raise ValueError("Interrupted migration did not recover atomically")
            persistence.Database(target)
            if snapshot(target) != after:
                raise ValueError("Interrupted migration did not converge")
    if before_sha != _sha(source) or media != media_inventory(source, source_users, local_users):
        raise ValueError("Source backup or media changed")
    return {"status": "PASS", "schema": [16, 17], "preserved_tables": len(after[1]),
            "preserved_rows": sum(count for count, _ in after[1].values()),
            "referenced_media": len(media), "disabled_cleanup": "REFUSED",
            "migration_replay": "PASS", "interrupt_before_and_after_commit": "PASS",
            "source_backup_unchanged": True, "commerce": result["commerce_rows_preserved"]}


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--internal-crash":
        return _crash(Path(sys.argv[3]), sys.argv[2])
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verified-backup-db", required=True, type=Path)
    ap.add_argument("--source-users-root", required=True, type=Path)
    ap.add_argument("--verified-media-root", required=True, type=Path)
    ap.add_argument("--private-scratch", required=True, type=Path)
    args = ap.parse_args()
    try:
        db, media, scratch = (p.resolve(strict=True) for p in (
            args.verified_backup_db, args.verified_media_root, args.private_scratch))
        if (not scratch.is_dir() or not media.is_dir() or db.is_relative_to(scratch)
                or media.is_relative_to(scratch) or scratch.is_relative_to(media)):
            raise ValueError("Use separate private scratch and backup trees")
        print(json.dumps(rehearse(db, scratch, args.source_users_root, media), sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as e:
        print(json.dumps({"status": "FAIL", "error_class": type(e).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
