"""Bounded cross-host reservation and CAS publication for a private S3 index."""

from __future__ import annotations

import re
from contextlib import contextmanager
from uuid import uuid4

from app.backup import BackupError


class UncertainMultipart(BackupError):
    """Upload outcome was not confirmed; preserve the fence until reconciled."""


def object_key(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def control(catalog, validate):
    if not isinstance(catalog, dict) or not {"pending", "retired"} <= catalog.keys():
        raise BackupError("Unknown recovery index; automatic import is forbidden")
    base = {k: v for k, v in catalog.items() if k not in ("pending", "retired")}
    points = validate(base)
    live = {p["slot"] for p in points}
    pending, retired = catalog["pending"], catalog["retired"]
    if (pending is not None and (not object_key(pending) or pending in live)
            or not isinstance(retired, list) or len(retired) > 6 or (pending is not None and retired)
            or any(not object_key(k) or k in live or k == pending for k in retired)
            or len(set(retired)) != len(retired)):
        raise BackupError("Unsafe recovery index state")
    return base


def finish_retirement(store, work, catalog, etag, validate):
    control(catalog, validate)
    if catalog["pending"] is not None:
        raise BackupError("Another writer owns the recovery reservation")
    if catalog["retired"]:
        # Already removed from the verified index by a successful CAS. Keys
        # are never reused, so concurrent retries of this delete are safe.
        for key in catalog["retired"]:
            store.discard(key)
        store.put_catalog({**catalog, "retired": []}, etag, work)


class Reservation:
    def __init__(self, store, work, validate, current, key):
        self.store, self.work, self.validate = store, work, validate
        self.current, self.key = current, key
        self.commit_started = False

    def owned(self):
        catalog, etag = self.store.get_catalog(self.work)
        if control(catalog, self.validate) != self.current or catalog["pending"] != self.key:
            raise BackupError("Recovery reservation was lost")
        return etag

    def publish(self, updated):
        points = self.validate(updated)
        kept = {p["slot"] for p in points}
        former = {p["slot"] for p in self.validate(self.current)}
        if not kept <= former | {self.key}:
            raise BackupError("Unreserved recovery object")
        index = {**updated, "pending": None, "retired": sorted((former | {self.key}) - kept)}
        control(index, self.validate)
        etag = self.owned()
        # Never auto-delete this key after the commit request was issued,
        # even if a subsequent read sees the old index or the response is lost.
        self.commit_started = True
        self.store.put_catalog(index, etag, self.work)

    def release_if_unpublished(self):
        if self.commit_started:
            return
        # A write with an ambiguous result (e.g. network timeout after PUT)
        # may have succeeded. Never delete based on the error alone.
        catalog, etag = self.store.get_catalog(self.work)
        control(catalog, self.validate)
        if catalog["pending"] == self.key:
            retire_pending(self.store, self.work, catalog, etag, self.validate, self.key)


def retire_pending(store, work, index, etag, validate, key):
    # CAS first, then delete. A stale read cannot remove a point which was
    # actually published while the operator was establishing the fence.
    store.put_catalog({**index, "pending": None, "retired": [key]}, etag, work)
    newest, fresh_etag = store.get_catalog(work)
    if newest["pending"] == key:
        raise BackupError("Recovery control read has not converged")
    if newest["pending"] is None:
        finish_retirement(store, work, newest, fresh_etag, validate)


@contextmanager
def reserve(store, work, validate):
    index, etag = store.get_catalog(work)
    finish_retirement(store, work, index, etag, validate)
    if index["retired"]:
        index, etag = store.get_catalog(work)
    current = control(index, validate)
    if index["pending"] is not None or index["retired"]:
        raise BackupError("Another writer owns the recovery reservation")
    key = uuid4().hex
    if key in {p["slot"] for p in validate(current)}:
        raise BackupError("Recovery object identity already exists")
    # No writes to the object namespace until this conditional write wins.
    store.put_catalog({**index, "pending": key}, etag, work)
    txn = Reservation(store, work, validate, current, key)
    txn.owned()
    try:
        yield txn
    except UncertainMultipart:
        raise
    except Exception:
        txn.release_if_unpublished()
        raise
    index, etag = store.get_catalog(work)
    # Another writer can start after our publish. It owns its own retirement
    # and will never overwrite a published key.
    if index["pending"] is None:
        finish_retirement(store, work, index, etag, validate)


def recover_interrupted_writer(store, work, validate, expected_key):
    """Operator-only after all possible writers are fenced and drained."""
    index, etag = store.get_catalog(work)
    control(index, validate)
    if not object_key(expected_key) or index["pending"] != expected_key:
        raise BackupError("Interrupted reservation does not match")
    store.abort_unfinished(expected_key)
    retire_pending(store, work, index, etag, validate, expected_key)
