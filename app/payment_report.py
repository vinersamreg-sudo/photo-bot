"""Read-only invoice-cohort payment journey report; never calls a provider."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import statistics
import sys

from app.feedback_report import ReportUnavailable, SAMARA, _SafeParser, _at, _owners, _time

STAGES = (
    ("checkout_created", "Checkout создан"),
    ("payment_link_opened", "Payment link открыт"),
    ("fail_return", "FailURL / payfail_ return"),
    ("success_url_return", "SuccessURL return"),
    ("result_url", "Подтверждённый ResultURL"),
    ("paid", "Покупка начислена"),
)


def build_report(db_path: Path, *, days: int, owner_platform_ids, at: datetime | None = None) -> dict:
    if not 1 <= days <= 3660:
        raise ReportUnavailable("Период должен быть от 1 до 3660 дней.")
    cutoff = at or datetime.now(timezone.utc)
    if cutoff.tzinfo is None:
        raise ReportUnavailable("Момент отчёта должен содержать часовой пояс.")
    cutoff = cutoff.astimezone(SAMARA)
    start = cutoff - timedelta(days=days)
    try:
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            owners = _owners(connection, owner_platform_ids)
            orders = {}
            paid_times = {}
            all_order_users = {}
            for row in connection.execute("SELECT id,user_id,created_at,paid_at FROM payment_orders WHERE provider='robokassa'"):
                created = _time(row["created_at"])
                if row["user_id"] not in owners:
                    all_order_users[row["id"]] = (row["user_id"], _time(row["paid_at"]))
                if row["user_id"] not in owners and created is not None and start <= created < cutoff:
                    orders[row["id"]] = {"user": row["user_id"], "created": created,
                        "paid_at": _time(row["paid_at"]), "events": [], "result": None, "grant": None}
            for row in connection.execute("SELECT order_id,event_type,reason,created_at FROM payment_audit ORDER BY id"):
                event_at = _time(row["created_at"])
                if row["order_id"] in orders and event_at is not None and event_at < cutoff:
                    orders[row["order_id"]]["events"].append((row["event_type"], event_at, row["reason"]))
            # An accepted, processed ResultURL and the linked grant are required;
            # paid_at alone or a browser return is not confirmation evidence.
            for row in connection.execute("""
                SELECT e.order_id,e.received_at,e.processed_at,g.created_at AS granted_at
                FROM payment_events e JOIN payment_webhooks w ON w.event_id=e.id
                JOIN payment_orders o ON o.id=e.order_id
                LEFT JOIN continuation_pack_grants g ON g.payment_order_id=e.order_id AND g.user_id=o.user_id
                WHERE e.provider='robokassa' AND e.event_type='result_url' AND e.status='processed'
                  AND w.signature_valid=1 AND w.http_status=200
            """):
                order = orders.get(row["order_id"])
                received, granted = _time(row["received_at"]), _time(row["granted_at"])
                processed = _time(row["processed_at"])
                identity = all_order_users.get(row["order_id"])
                if identity and received and granted and processed and identity[1] and max(received, granted, processed, identity[1]) < cutoff:
                    confirmed_at = max(received, granted, processed, identity[1])
                    key = row["order_id"]
                    previous = paid_times.get(key)
                    paid_times[key] = (identity[0], min(previous[1], confirmed_at) if previous else confirmed_at)
                if order and received is not None and received < cutoff:
                    order["result"] = min(order["result"], received) if order["result"] else received
                    if granted is not None and granted < cutoff and processed is not None and processed < cutoff and order["paid_at"] is not None and order["paid_at"] < cutoff:
                        candidate = max(granted, received, processed, order["paid_at"])
                        order["grant"] = min(order["grant"], candidate) if order["grant"] else candidate
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        raise ReportUnavailable("Отчёт недоступен: существующая SQLite не читается или схема несовместима.") from None

    invoices = Counter()
    authors = {key: set() for key, _ in STAGES}
    fail_url = set()
    max_fail = set()
    recovered = set()
    unresolved = set()
    recovered_users = set()
    failure_users = set()
    users_paid_after_fail_any_invoice = set()
    users_already_paid_at_fail = set()
    reopened_invoices = set()
    reopen_hits = 0
    replacement_invoices = 0
    observed_invoices = 0
    latencies = []
    for order_id, order in orders.items():
        events = order["events"]
        names = {event[0] for event in events}
        observed = any(_is_journey(event[2]) for event in events)
        observed_invoices += observed
        failures = [event[1] for event in events if event[0] in {"fail_url_return", "max_payfail_return"}]
        stages = {"checkout_created"}
        if "payment_link_opened" in names:
            stages.add("payment_link_opened")
        if failures:
            stages.add("fail_return")
            failure_users.add(order["user"])
            if order["grant"] and order["grant"] <= min(failures):
                users_already_paid_at_fail.add(order["user"])
            if any(user == order["user"] and min(failures) < paid_at for user, paid_at in paid_times.values()):
                users_paid_after_fail_any_invoice.add(order["user"])
        if "success_url_return" in names:
            stages.add("success_url_return")
        if order["result"]:
            stages.add("result_url")
        if order["grant"]:
            stages.add("paid")
            latencies.append((order["grant"] - order["created"]).total_seconds())
        for key in stages:
            invoices[key] += 1
            authors[key].add(order["user"])
        if "fail_url_return" in names:
            fail_url.add(order_id)
        if "max_payfail_return" in names:
            max_fail.add(order_id)
        if failures:
            if order["grant"] and min(failures) < order["grant"]:
                recovered.add(order_id)
                recovered_users.add(order["user"])
            elif not order["grant"]:
                unresolved.add(order_id)
        hits = sum(event[0] == "payment_link_reopened" for event in events)
        reopen_hits += hits
        if hits:
            reopened_invoices.add(order_id)
        replacement_invoices += "checkout_created_after_failure" in names
    return {
        "from": start.isoformat(), "to": cutoff.isoformat(), "timezone": "Europe/Samara",
        "cohort": "robokassa invoices created in [from,to); outcomes observed before cutoff",
        "owner_exclusion": "verified_all_configured_max_owners",
        "stages": {key: {"invoices": invoices[key], "users": len(authors[key])} for key, _ in STAGES},
        "fail_url_invoices": len(fail_url), "max_payfail_invoices": len(max_fail),
        "paid_after_fail_same_invoice": len(recovered), "unpaid_after_fail_invoices": len(unresolved),
        "users_with_fail_return": len(failure_users), "users_paid_after_fail_same_invoice": len(recovered_users),
        "users_paid_after_fail_any_invoice": len(users_paid_after_fail_any_invoice),
        "users_without_payment_after_fail": len(failure_users - users_paid_after_fail_any_invoice - users_already_paid_at_fail),
        "late_fail_returns_after_paid": sum(bool(order["grant"] and any(event[0] in {"fail_url_return", "max_payfail_return"} and event[1] >= order["grant"] for event in order["events"])) for order in orders.values()),
        "reopened_invoices": len(reopened_invoices), "repeat_link_open_requests": reopen_hits,
        "new_invoices_after_failure": replacement_invoices,
        "median_checkout_to_paid_seconds": round(statistics.median(latencies), 3) if latencies else None,
        "journey_instrumented_invoices": observed_invoices,
        "payment_methods": "NOT MEASURABLE",
        "caveats": [
            "Stages are optional observations, not a mandatory ordered funnel: ResultURL can precede browser return.",
            "Link opens are HTTP hits to Ravuna, potentially including browser previews; not bank visits or provider attempts.",
            "First browser return per route/invoice is recorded; SuccessURL auto-refreshes are deduplicated.",
            "Historical link/return telemetry cannot be reconstructed; missing observations are not evidence of abandonment.",
            "PaymentMethod is not stored from unsigned classic callback fields; verified ResultUrl2 is not enabled by this change.",
            "Unpaid is as of cutoff; FailURL is not a bank decline and a later ResultURL may still arrive.",
        ],
    }


def _is_journey(value) -> bool:
    try:
        return json.loads(value or "{}").get("telemetry_version") == 1
    except (ValueError, AttributeError):
        return False


def render_markdown(report: dict) -> str:
    lines = ["# Платёжный путь Ravuna", f"Период создания счетов: {report['from']} → {report['to']} (Samara).",
        "Владелец/тест исключён. Один invoice = одна покупка, независимо от числа открытий.",
        "", "| Наблюдение | Invoices | Уникальные пользователи |", "|---|---:|---:|"]
    for key, label in STAGES:
        value = report["stages"][key]
        lines.append(f"| {label} | {value['invoices']} | {value['users']} |")
    lines.extend(["", f"FailURL: {report['fail_url_invoices']}; MAX payfail_: {report['max_payfail_invoices']} счетов.",
        f"После fail оплатили тот же invoice: {report['paid_after_fail_same_invoice']}; остались неоплаченными: {report['unpaid_after_fail_invoices']}.",
        f"Пользователи, оплатившие после fail (включая новый invoice): {report['users_paid_after_fail_any_invoice']}; без последующей оплаты: {report['users_without_payment_after_fail']}.",
        f"Повторные открытия: {report['repeat_link_open_requests']} на {report['reopened_invoices']} счетах (не provider attempts).",
        f"Новые invoices после fail: {report['new_invoices_after_failure']}.",
        f"Поздние fail returns уже оплаченных счетов (не потерянные покупки): {report['late_fail_returns_after_paid']}.",
        f"Медиана checkout → начисленная покупка: {report['median_checkout_to_paid_seconds']} секунд.",
        f"Счета с новой journey telemetry: {report['journey_instrumented_invoices']}.",
        "PaymentMethod: NOT MEASURABLE (не подтверждён текущим контрактом callback).", "", "Ограничения:"])
    lines.extend(f"- {caveat}" for caveat in report["caveats"])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = _SafeParser(description="Read-only payment-report; MAX_OWNER_USER_IDS исключается fail-closed.")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--at", type=_at)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)
    try:
        from app.config import load_settings
        settings = load_settings()
        report = build_report(args.db or settings.database_path, days=args.days,
            owner_platform_ids=settings.max_owner_user_ids, at=args.at)
    except ReportUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError):
        print("Отчёт недоступен: проверьте конфигурацию; значения не выводятся.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else render_markdown(report),
          end="\n" if args.format == "json" else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
