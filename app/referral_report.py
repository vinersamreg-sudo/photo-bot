"""Read-only, owner-excluded referral ledger section for the weekly report."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys

from app.database import ReadOnlyDatabase
from app.feedback_report import ReportUnavailable, SAMARA, _SafeParser, _at, _owners, _time
from app.referrals import ReferralService

NM = "NOT MEASURABLE"


def build_report(path: Path, *, days=7, owner_platform_ids=(), at=None):
    if not 1 <= days <= 3660:
        raise ReportUnavailable("Период должен быть от 1 до 3660 дней.")
    end = at or datetime.now(timezone.utc)
    if end.tzinfo is None:
        raise ReportUnavailable("Нужен cutoff с часовым поясом.")
    end = end.astimezone(SAMARA)
    start = end - timedelta(days=days)
    inside = lambda value: bool(_time(value) and start <= _time(value) < end)
    try:
        with ReadOnlyDatabase(path).read() as c:
            c.execute("BEGIN")
            owners = _owners(c, owner_platform_ids)
            owner_subjects = {ReferralService.subject(str(value)) for value in owner_platform_ids}
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            relations = [dict(r) for r in c.execute("SELECT * FROM referral_relationships")
                         if r["invitee_user_id"] not in owners and ReferralService.subject(r["invitee_platform_user_id"]) not in owner_subjects
                         and _time(r["started_at"]) and _time(r["started_at"]) < end]
            eligible = {r["invitee_user_id"]: r for r in relations if r["invitee_user_id"]}
            shares = {r["platform_user_id"] for r in c.execute("SELECT * FROM attribution_events WHERE event_type='share_opened'")
                      if r["user_id"] not in owners and ReferralService.subject(r["platform_user_id"]) not in owner_subjects and inside(r["created_at"])}
            metadata = dict(c.execute("SELECT name,first_enabled_at FROM referral_program_metadata")) if "referral_program_metadata" in tables else {}
            diagnostics = [dict(r) for r in c.execute("SELECT * FROM referral_diagnostics")
                           if r["user_id"] not in owners and r["subject_hash"] not in owner_subjects and inside(r["created_at"])] if "referral_diagnostics" in tables else None
            first_success = {}
            for r in c.execute("""SELECT a.user_id,c.consumed_at FROM generation_attempts a
                JOIN generation_credit_reservations c ON c.attempt_id=a.id AND c.status='consumed'
                WHERE a.status='succeeded'"""):
                done = _time(r["consumed_at"])
                if r["user_id"] in eligible and done and done < end and done >= _time(eligible[r["user_id"]]["started_at"]):
                    first_success[r["user_id"]] = min(first_success.get(r["user_id"], done), done)
            grants = [dict(r) for r in c.execute("SELECT * FROM referral_bonus_grants")
                      if r["user_id"] not in owners and _time(r["created_at"]) and _time(r["created_at"]) < end] if "referral_bonus_grants" in tables else None
            legacy = [dict(r) for r in c.execute("SELECT * FROM bonus_credit_transactions") if r["user_id"] not in owners and inside(r["created_at"])]
            paid = {}
            for r in c.execute("""SELECT o.id,o.user_id,o.amount_minor,o.paid_at,e.received_at,e.processed_at,g.created_at AS granted
                FROM payment_orders o JOIN payment_events e ON e.order_id=o.id JOIN payment_webhooks w ON w.event_id=e.id
                JOIN continuation_pack_grants g ON g.payment_order_id=o.id AND g.user_id=o.user_id
                WHERE o.provider='robokassa' AND e.event_type='result_url' AND e.status='processed'
                  AND w.signature_valid=1 AND w.http_status=200 AND o.currency='RUB'"""):
                times = [_time(r[key]) for key in ("paid_at", "received_at", "processed_at", "granted")]
                if r["user_id"] in owners or not all(times):
                    continue
                done = max(times)
                if done < end and (r["id"] not in paid or done < paid[r["id"]][2]):
                    paid[r["id"]] = (r["user_id"], r["amount_minor"], done)
            first_paid = {}
            for invoice, (user, _, done) in paid.items():
                first_paid[user] = min(first_paid.get(user, (done, invoice)), (done, invoice))
            selected_payments = [(invoice, user, amount, done) for invoice, (user, amount, done) in paid.items()
                if user in eligible and start <= done < end and done >= _time(eligible[user]["started_at"])]
            bonus = _bonus_usage(c, grants, inside)
            activity = defaultdict(set)
            if "referral_activity" in tables:
                for r in c.execute("SELECT user_id,received_at FROM referral_activity"):
                    done = _time(r["received_at"])
                    if r["user_id"] in eligible and done and done < end:
                        activity[r["user_id"]].add(done.astimezone(SAMARA).date())
            retention = {}
            for delay in (1, 7, 14):
                aged = []
                for user, relation in eligible.items():
                    registered = _time(relation.get("registered_at"))
                    if relation.get("program_version", 1) == 2 and registered and start <= registered < end and metadata.get("activity_start") and registered >= _time(metadata["activity_start"]):
                        target = registered.astimezone(SAMARA).date() + timedelta(days=delay)
                        if target < end.date():
                            aged.append((user, target))
                retention[f"D+{delay}"] = {"eligible": len(aged), "returned": sum(day in activity[user] for user, day in aged)} if "referral_activity" in tables else NM
            costs = _free_cost(c, eligible, grants, owners, inside)
            return {
                "from": start.isoformat(), "to": end.isoformat(), "timezone": "Europe/Samara",
                "database_access": "mode=ro; query_only=ON; consistent read transaction", "owner_test_exclusion": "verified MAX_OWNER_USER_IDS",
                "v2_first_enabled_at": _time(metadata["v2_activation"]).astimezone(SAMARA).isoformat() if metadata.get("v2_activation") else None,
                "unique_invitation_screen_users": len(shares), "invitations_actually_sent": NM,
                "registrations_by_version": {"v1": NM, "v2": sum(inside(r.get("registered_at")) and r.get("program_version", 1) == 2 for r in relations)},
                "legacy_linked_accounts": sum(inside(r.get("registered_at")) and r.get("program_version", 1) == 1 for r in relations),
                "legacy_link_starts": sum(inside(r["started_at"]) and r.get("program_version", 1) == 1 for r in relations),
                "legacy_confirmed_registration_timestamps": NM,
                "first_delivered_success_users": sum(start <= done < end for done in first_success.values()),
                "legacy_v1_rewards": {"grants": len(legacy), "edits": sum(r["quantity"] for r in legacy), "originals": 0, "consumed_edits": NM},
                "v2_bonus": bonus, "diagnostics": dict(Counter((r["event_type"] + ":" + r["reason"]) for r in diagnostics)) if diagnostics is not None else NM,
                "first_purchases": sum((done, invoice) == first_paid[user] for invoice, user, _, done in selected_payments),
                "repeat_purchases": sum((done, invoice) != first_paid[user] for invoice, user, _, done in selected_payments),
                "confirmed_gross_revenue_minor": sum(amount for _, _, amount, _ in selected_payments),
                "repeat_users": retention, "free_edit_cost": costs,
                "warnings": ["Review registration bursts; a burst alone is not evidence of fraud and does not cap grants."] if diagnostics and any(r["event_type"] == "suspected" for r in diagnostics) else [],
                "limitations": ["Invitation screen/clipboard access does not prove that a link was sent.",
                    "V1 reward quantities/history are not upgraded; legacy registration dates and bonus consumption provenance can be unavailable.",
                    "D+N covers only V2 cohorts: a completed Samara calendar day after recorded registration; unaged cohorts are excluded. V1 lacks this activity telemetry. Activity uses server-received authenticated events, not client open/view tracking.",
                    "Gross confirmed revenue is not net profit. Costs are provider telemetry estimates, not billing.",
                    "Distinct authenticated MAX identities do not prove distinct humans; MAX does not provide authoritative device/account-age identity proof here."],
            }
    except (sqlite3.Error, OSError):
        raise ReportUnavailable("Отчёт недоступен: несовместимый read-only snapshot.") from None


def _bonus_usage(c, grants, inside):
    if grants is None:
        return NM
    issued = Counter()
    consumed_edits = 0
    consumed_originals = 0
    for grant in grants:
        if inside(grant["created_at"]):
            issued[f"{grant['beneficiary']}_{grant['bonus_type']}"] += grant["quantity"]
        if grant["bonus_type"] == "edits":
            if not grant["credit_lot_id"]:
                consumed_edits = NM
            elif isinstance(consumed_edits, int):
                consumed_edits += sum(inside(r[0]) for r in c.execute("SELECT consumed_at FROM generation_credit_reservations WHERE lot_id=? AND status='consumed'", (grant["credit_lot_id"],)))
        else:
            rows = c.execute("SELECT e.status,e.consumed_at FROM referral_bonus_entitlements m LEFT JOIN unlock_entitlements e ON e.id=m.entitlement_id WHERE m.grant_id=?", (grant["id"],)).fetchall()
            if len(rows) != grant["quantity"] or any(r["status"] is None for r in rows):
                consumed_originals = NM
            elif isinstance(consumed_originals, int):
                consumed_originals += sum(r["status"] == "consumed" and inside(r["consumed_at"]) for r in rows)
    return {"granted": dict(issued), "consumed_edits": consumed_edits, "consumed_originals": consumed_originals}


def _free_cost(c, eligible, grants, owners, inside):
    lots = {r["credit_lot_id"] for r in grants or [] if r["credit_lot_id"]}
    values = []
    unknown = 0
    for r in c.execute("""SELECT a.user_id,a.estimated_cost,c.consumed_at,c.lot_id,l.source_type
        FROM generation_attempts a JOIN generation_credit_reservations c ON c.attempt_id=a.id AND c.status='consumed'
        JOIN generation_credit_lots l ON l.id=c.lot_id WHERE a.status='succeeded'"""):
        if r["user_id"] in owners or not inside(r["consumed_at"]):
            continue
        if r["lot_id"] not in lots and not (r["user_id"] in eligible and r["source_type"] == "initial_free"):
            continue
        if r["estimated_cost"] is None or not math.isfinite(float(r["estimated_cost"])) or float(r["estimated_cost"]) < 0:
            unknown += 1
        else:
            values.append(float(r["estimated_cost"]))
    total = round(sum(values), 6)
    return {"basis": "estimated_cost telemetry, USD; NOT actual API billing", "known_attempts": len(values),
            "unknown_attempts": unknown, "known_estimate_usd": total,
            "total_estimate_usd": NM if unknown else total,
            "review_alert": "ESTIMATED_FREE_EDIT_COST_REVIEW" if total >= 5 else None}


def render_markdown(report):
    return "# Реферальная программа Ravuna\n\n" + "\n".join(f"- {key}: {json.dumps(value, ensure_ascii=False)}" for key, value in report.items()) + "\n"


def main(argv=None):
    parser = _SafeParser(description="Read-only referral-report with proven owner/test exclusion")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--at", type=_at)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)
    from app.config import load_settings
    try:
        settings = load_settings()
        report = build_report(args.db or settings.database_path, days=args.days, owner_platform_ids=settings.max_owner_user_ids, at=args.at)
    except (ReportUnavailable, ValueError):
        print("Реферальный отчёт недоступен: проверьте read-only snapshot и доказанный owner/test filter.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
