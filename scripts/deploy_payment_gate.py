"""Fail on payment corruption or new reconciliation warnings during deploy."""

import argparse
import hashlib
import json
from pathlib import Path

from app.config import load_settings
from app.database import ReadOnlyDatabase
from app.payment_admin import payment_reconciliation_summary


def fingerprint_ids(rows):
    return sorted(hashlib.sha256(str(row[0]).encode()).hexdigest() for row in rows)


def check(mode, snapshot, report, pending, rejected):
    if report["status"] not in {"OK", "WARNING"}:
        raise ValueError("critical payment reconciliation failure")
    if len(pending) != report["failed_original_delivery"] or len(rejected) != report["rejected_callbacks_recent"]:
        raise ValueError("payment reconciliation changed during snapshot")
    current = {"delivery_pending": pending, "recent_rejected": rejected}
    if mode == "capture":
        # Historical unfulfilled originals remain visible; a recent callback
        # rejection is not an approved baseline for this release.
        if rejected:
            raise ValueError("recent rejected payment callback")
        with snapshot.open("x", encoding="utf-8") as stream:
            json.dump(current, stream)
    else:
        before = json.loads(snapshot.read_text(encoding="utf-8"))
        if rejected or not set(pending).issubset(before["delivery_pending"]):
            raise ValueError("new payment reconciliation warning during deploy")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("capture", "verify"))
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    database = ReadOnlyDatabase(load_settings().database_path)
    report = payment_reconciliation_summary(database)
    print("payment-reconciliation", json.dumps(report, sort_keys=True))
    with database.read() as connection:
        pending = fingerprint_ids(connection.execute(
            "SELECT id FROM payment_orders WHERE status='delivery_pending'"
        ))
        rejected = fingerprint_ids(connection.execute(
            "SELECT id FROM payment_events WHERE status='rejected' "
            "AND datetime(received_at)>=datetime('now','-24 hours')"
        ))
    check(args.mode, args.snapshot, report, pending, rejected)


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None
