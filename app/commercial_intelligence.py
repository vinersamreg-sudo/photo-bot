"""Deterministic, privacy-safe commercial intelligence from a read-only SQLite snapshot."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.database import CURRENT_SCHEMA_VERSION, ReadOnlyDatabase, schema_version


PAID_ORDER_STATUSES = {
    "paid",
    "delivery_pending",
    "delivered",
    "refund_pending",
    "partially_refunded",
    "refunded",
}
SUCCESS_STATUS = "succeeded"
NOT_MEASURABLE = "NOT MEASURABLE"


@dataclass(frozen=True)
class AuditWindow:
    label: str
    start: datetime
    end: datetime
    timezone_name: str

    def contains(self, value: datetime | None) -> bool:
        return value is not None and self.start <= value < self.end


def _timezone(timezone_name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name == "Europe/Samara":
            return timezone(timedelta(hours=4), name="Europe/Samara")
        raise ValueError(f"timezone data unavailable for {timezone_name!r}") from None


def parse_boundary(value: str, timezone_name: str) -> datetime:
    """Parse an ISO date/datetime into an aware UTC boundary."""
    zone = _timezone(timezone_name)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed = datetime.combine(date.fromisoformat(value), time.min, tzinfo=zone)
        else:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=zone)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date/datetime: {value}") from exc
    return parsed.astimezone(timezone.utc)


def masked_id(value: str) -> str:
    """Return a stable, non-reversible display identifier."""
    digest = hashlib.sha256(f"ravuna-commercial-v1:{value}".encode("utf-8")).hexdigest()
    return f"u_{digest[:10]}"


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percent(numerator: int | float, denominator: int | float) -> float | None:
    return round(float(numerator) * 100.0 / float(denominator), 2) if denominator else None


def _percentile(values: Iterable[int | float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 2)
    result = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(result, 2)


def _median_seconds(values: Iterable[timedelta]) -> float | None:
    seconds = [value.total_seconds() for value in values]
    return round(float(median(seconds)), 2) if seconds else None


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _safe_rows(
    connection: sqlite3.Connection, tables: set[str], table: str
) -> list[dict[str, Any]]:
    return _rows(connection, table) if table in tables else []


JTBD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("product_commercial", ("товар", "карточк", "маркетплейс", "продукт", "упаковк", "product")),
    ("background", ("фон", "background", "задний план")),
    ("clothing_style", ("одеж", "плать", "костюм", "рубаш", "образ", "наряд")),
    ("restoration", ("рестав", "старое фото", "старую фотограф", "восстанов")),
    ("retouch_portrait", ("ретуш", "лицо", "кож", "портрет", "морщ", "причес")),
    ("remove_object", ("удали", "убери", "удалить", "лишн")),
    ("composition_transfer", ("перенеси", "добавь", "вставь", "объедини", "из фото 2", "со второго")),
    ("interior", ("интерьер", "комнат", "мебел", "стен", "кухн")),
    ("enhancement", ("улучш", "ярче", "темнее", "резк", "качество", "цветокор")),
)


def classify_prompt(prompt: str) -> str:
    normalized = " ".join(prompt.casefold().split())
    for name, needles in JTBD_RULES:
        if any(needle in normalized for needle in needles):
            return name
    return "other"


class CommercialIntelligence:
    """Calculate all report sections from one immutable database view."""

    REQUIRED_TABLES = {
        "users",
        "demo_sessions",
        "generation_attempts",
        "payment_orders",
        "payment_events",
        "continuation_pack_grants",
        "generation_credit_lots",
        "generation_credit_reservations",
        "credit_ledger",
        "unlock_entitlements",
    }
    OPTIONAL_TABLES = {
        "payment_attempts",
        "product_events",
        "attribution_profiles",
    }

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def calculate(self, windows: list[AuditWindow]) -> dict[str, Any]:
        database = ReadOnlyDatabase(self.database_path)
        with database.read() as connection:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            version = schema_version(connection)
            if version > CURRENT_SCHEMA_VERSION:
                raise ValueError(
                    f"database schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}"
                )
            tables = _table_names(connection)
            missing = sorted(self.REQUIRED_TABLES - tables)
            if missing:
                raise ValueError("missing required tables: " + ", ".join(missing))
            data = {
                name: _safe_rows(connection, tables, name)
                for name in sorted(self.REQUIRED_TABLES | self.OPTIONAL_TABLES)
            }

        return {
            "report_version": 1,
            "source": {
                "database": "sqlite_snapshot",
                "sqlite_quick_check": quick_check,
                "schema_version": version,
                "read_only": True,
                "authority": "payment/order/grant ledgers and generation_attempts; analytics supplementary",
            },
            "windows": [self._window_report(data, window) for window in windows],
            "privacy": {
                "full_ids_in_output": False,
                "full_prompts_in_output": False,
                "secrets_in_output": False,
                "identifier_format": "stable SHA-256-derived mask",
            },
        }

    def _window_report(
        self, data: dict[str, list[dict[str, Any]]], window: AuditWindow
    ) -> dict[str, Any]:
        users = data["users"]
        sessions = data["demo_sessions"]
        attempts = data["generation_attempts"]
        orders = data["payment_orders"]
        events = data["payment_events"]
        grants = data["continuation_pack_grants"]
        payment_attempts = data.get("payment_attempts", [])
        reservations = data["generation_credit_reservations"]
        credit_ledger = data["credit_ledger"]
        entitlements = data["unlock_entitlements"]
        product_events = data.get("product_events", [])
        attempt_time = {row["id"]: _dt(row.get("started_at")) for row in attempts}
        session_user = {row["id"]: row["user_id"] for row in sessions}
        grants_by_order = {row["payment_order_id"]: row for row in grants}
        confirmed = [
            row
            for row in orders
            if row["id"] in grants_by_order
            and row.get("paid_at")
            and str(row.get("status")) in PAID_ORDER_STATUSES
        ]
        confirmed.sort(key=lambda row: (_dt(row["paid_at"]) or window.end, row["id"]))
        purchases_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in confirmed:
            purchases_by_user[str(row["user_id"])].append(row)
        purchases_as_of_end: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for user_id, rows in purchases_by_user.items():
            purchases_as_of_end[user_id] = [
                row for row in rows if (_dt(row["paid_at"]) or window.end) < window.end
            ]

        users_window = {row["id"] for row in users if window.contains(_dt(row["created_at"]))}
        attempts_window = [row for row in attempts if window.contains(attempt_time[row["id"]])]
        edit_users = {str(row["user_id"]) for row in attempts_window}
        success_window = [row for row in attempts_window if row["status"] == SUCCESS_STATUS]
        success_users = {str(row["user_id"]) for row in success_window}

        uploaded_users: set[str] = set()
        for row in product_events:
            if row.get("event_type") == "photo_uploaded" and window.contains(_dt(row.get("created_at"))):
                user_id = session_user.get(row.get("session_id"))
                if user_id:
                    uploaded_users.add(str(user_id))
        if not uploaded_users:
            uploaded_users = {
                str(row["user_id"])
                for row in sessions
                if window.contains(_dt(row.get("started_at") or row.get("created_at")))
            }

        orders_window = [row for row in orders if window.contains(_dt(row["created_at"]))]
        checkout_users = {str(row["user_id"]) for row in orders_window}
        paid_window = [row for row in confirmed if window.contains(_dt(row["paid_at"]))]
        payer_users = {str(row["user_id"]) for row in paid_window}

        first_paid_at = {
            user_id: _dt(rows[0]["paid_at"])
            for user_id, rows in purchases_by_user.items()
            if rows
        }
        first_time_payers = {
            user_id for user_id in payer_users if window.contains(first_paid_at.get(user_id))
        }
        repeat_payers = {
            user_id
            for user_id in payer_users
            if any(
                window.contains(_dt(row["paid_at"])) and index > 0
                for index, row in enumerate(purchases_by_user[user_id])
            )
        }

        funnel = {
            "new_users": len(users_window),
            "uploaded_users": len(uploaded_users),
            "edit_users": len(edit_users),
            "successful_result_users": len(success_users),
            "checkout_users": len(checkout_users),
            "unique_payers": len(payer_users),
            "first_time_payers": len(first_time_payers),
            "repeat_payers": len(repeat_payers),
            "stage_conversions_percent": {
                "new_to_upload": _percent(len(users_window & uploaded_users), len(users_window)),
                "upload_to_edit": _percent(len(uploaded_users & edit_users), len(uploaded_users)),
                "edit_to_success": _percent(len(edit_users & success_users), len(edit_users)),
                "checkout_to_payer": _percent(len(checkout_users & payer_users), len(checkout_users)),
            },
        }

        revenue_by_user: Counter[str] = Counter()
        package_orders: Counter[str] = Counter()
        package_users: dict[str, set[str]] = defaultdict(set)
        new_revenue = returning_revenue = 0
        for row in paid_window:
            amount = int(row["amount_minor"])
            user_id = str(row["user_id"])
            revenue_by_user[user_id] += amount
            code = str(row.get("product_code") or f"amount_{amount}")
            package_orders[code] += 1
            package_users[code].add(user_id)
            if _dt(row["paid_at"]) == first_paid_at.get(user_id):
                new_revenue += amount
            else:
                returning_revenue += amount
        revenue_minor = sum(revenue_by_user.values())
        ranked_revenue = sorted(revenue_by_user.values(), reverse=True)
        historic_totals = Counter(
            {
                user_id: sum(int(row["amount_minor"]) for row in rows)
                for user_id, rows in purchases_by_user.items()
            }
        )
        historic_heavy_user = (
            sorted(historic_totals.items(), key=lambda item: (-item[1], masked_id(item[0])))[0][0]
            if historic_totals
            else None
        )
        revenue = {
            "confirmed_revenue_rub": round(revenue_minor / 100, 2),
            "confirmed_orders": len(paid_window),
            "unique_buyers": len(payer_users),
            "arppu_rub": round(revenue_minor / 100 / len(payer_users), 2) if payer_users else None,
            "packages": {
                code: {
                    "orders": package_orders[code],
                    "unique_buyers": len(package_users[code]),
                    "revenue_rub": round(
                        sum(
                            int(row["amount_minor"])
                            for row in paid_window
                            if str(row.get("product_code") or f"amount_{row['amount_minor']}") == code
                        )
                        / 100,
                        2,
                    ),
                }
                for code in sorted(package_orders)
            },
            "new_payer_revenue_rub": round(new_revenue / 100, 2),
            "returning_payer_revenue_rub": round(returning_revenue / 100, 2),
            "top_1_concentration_percent": _percent(sum(ranked_revenue[:1]), revenue_minor),
            "top_3_concentration_percent": _percent(sum(ranked_revenue[:3]), revenue_minor),
            "historic_heavy_user": masked_id(historic_heavy_user) if historic_heavy_user else None,
            "historic_heavy_user_concentration_percent": _percent(
                revenue_by_user.get(historic_heavy_user or "", 0), revenue_minor
            ),
            "refunded_rub": round(
                sum(int(row.get("refunded_amount_minor") or 0) for row in paid_window) / 100, 2
            ),
        }

        success_by_user: dict[str, list[datetime]] = defaultdict(list)
        for row in attempts:
            stamp = _dt(row.get("completed_at")) or attempt_time[row["id"]]
            if row["status"] == SUCCESS_STATUS and stamp and stamp < window.end:
                success_by_user[str(row["user_id"])].append(stamp)
        for values in success_by_user.values():
            values.sort()
        first_success_cohort = {
            user_id: values
            for user_id, values in success_by_user.items()
            if values and window.contains(values[0])
        }
        payment_cohort = {
            user_id: rows
            for user_id, rows in purchases_as_of_end.items()
            if rows and window.contains(_dt(rows[0]["paid_at"]))
        }

        def aged_rate(
            cohort: dict[str, Any], first: Callable[[Any], datetime], predicate: Callable[[Any, int], bool], days: int
        ) -> dict[str, Any]:
            eligible = {
                user_id: value
                for user_id, value in cohort.items()
                if first(value) + timedelta(days=days) <= window.end
            }
            matched = sum(1 for value in eligible.values() if predicate(value, days))
            return {
                "eligible_users": len(eligible),
                "matched_users": matched,
                "conversion_percent": _percent(matched, len(eligible)),
            }

        repeat_usage: dict[str, Any] = {}
        repeat_payment: dict[str, Any] = {}
        for days in (7, 14, 30):
            repeat_usage[str(days)] = aged_rate(
                payment_cohort,
                lambda rows: _dt(rows[0]["paid_at"]),
                lambda rows, limit: any(
                    first_paid_at[str(rows[0]["user_id"])] < success <= first_paid_at[str(rows[0]["user_id"])] + timedelta(days=limit)
                    for success in success_by_user.get(str(rows[0]["user_id"]), [])
                ),
                days,
            )
            repeat_payment[str(days)] = aged_rate(
                payment_cohort,
                lambda rows: _dt(rows[0]["paid_at"]),
                lambda rows, limit: len(rows) > 1
                and (_dt(rows[1]["paid_at"]) - _dt(rows[0]["paid_at"])) <= timedelta(days=limit),
                days,
            )

        window_success_counts = Counter(str(row["user_id"]) for row in success_window)
        active_days: dict[str, set[date]] = defaultdict(set)
        zone = _timezone(window.timezone_name)
        for row in success_window:
            completed = _dt(row.get("completed_at")) or attempt_time[row["id"]]
            if completed:
                active_days[str(row["user_id"])].add(completed.astimezone(zone).date())
        second_success_deltas = [
            values[1] - values[0] for values in first_success_cohort.values() if len(values) >= 2
        ]
        second_payment_deltas = [
            _dt(rows[1]["paid_at"]) - _dt(rows[0]["paid_at"])
            for rows in payment_cohort.values()
            if len(rows) >= 2
        ]
        cumulative_repeat_buyers = sum(
            1
            for rows in purchases_as_of_end.values()
            if len(rows) >= 2
        )
        retention = {
            "first_success_to_second_success": {
                "cohort_users": len(first_success_cohort),
                "second_success_users": sum(1 for values in first_success_cohort.values() if len(values) >= 2),
                "conversion_percent": _percent(
                    sum(1 for values in first_success_cohort.values() if len(values) >= 2),
                    len(first_success_cohort),
                ),
            },
            "payer_to_repeat_usage_within_days": repeat_usage,
            "first_payment_to_second_payment_within_days": repeat_payment,
            "median_seconds_to_second_success": _median_seconds(second_success_deltas),
            "median_seconds_to_second_payment": _median_seconds(second_payment_deltas),
            "successful_edit_user_thresholds": {
                str(threshold): sum(1 for count in window_success_counts.values() if count >= threshold)
                for threshold in (2, 5, 10)
            },
            "active_day_user_thresholds": {
                str(threshold): sum(1 for days in active_days.values() if len(days) >= threshold)
                for threshold in (2, 3, 5)
            },
            "independent_users_with_at_least_2_purchases_as_of_end": cumulative_repeat_buyers,
        }

        latencies = [int(row["duration_ms"]) for row in success_window if row.get("duration_ms") is not None]
        failures = [row for row in attempts_window if row["status"] != SUCCESS_STATUS]
        failures_by_error = Counter(str(row.get("error_type") or row["status"]) for row in failures)
        failures_by_provider_model: Counter[str] = Counter(
            f"{row.get('provider') or 'unknown'}/{row.get('model') or 'unknown'}" for row in failures
        )
        source_counts = Counter(
            "2_image" if row.get("secondary_source_path") else "1_image" for row in attempts_window
        )
        usage = {
            "attempts": len(attempts_window),
            "successes": len(success_window),
            "failures": len(failures),
            "failure_rate_percent": _percent(len(failures), len(attempts_window)),
            "source_counts": dict(sorted(source_counts.items())),
            "successful_latency_ms": {
                "median": round(float(median(latencies)), 2) if latencies else None,
                "p90": _percentile(latencies, 0.90),
                "min": min(latencies) if latencies else None,
                "max": max(latencies) if latencies else None,
            },
            "failures_by_error_class": dict(sorted(failures_by_error.items())),
            "failures_by_provider_model": dict(sorted(failures_by_provider_model.items())),
            "successful_edits_per_active_user": round(len(success_window) / len(edit_users), 2) if edit_users else None,
        }

        grant_by_order = {str(row["payment_order_id"]): row for row in grants}
        confirmed_order_ids = {str(row["id"]) for row in confirmed}
        result_events = [
            row
            for row in events
            if row.get("event_type") == "result_url"
            and row.get("status") == "processed"
            and str(row.get("order_id")) in confirmed_order_ids
            and str(row.get("order_id")) in grant_by_order
            and window.contains(_dt(row.get("received_at")))
        ]
        result_to_grant: list[float] = []
        for event in result_events:
            grant = grant_by_order.get(str(event.get("order_id")))
            if grant:
                received = _dt(event.get("received_at"))
                committed = _dt(grant.get("created_at"))
                if received and committed and committed >= received:
                    result_to_grant.append((committed - received).total_seconds())
        duplicate_grants = sum(count - 1 for count in Counter(row["payment_order_id"] for row in grants).values() if count > 1)
        duplicate_result_events = sum(
            count - 1
            for count in Counter(
                str(row["order_id"])
                for row in events
                if row.get("order_id")
                and row.get("event_type") == "result_url"
                and row.get("status") == "processed"
            ).values()
            if count > 1
        )
        paid_missing_grant = sum(
            1
            for row in orders
            if str(row.get("status")) in PAID_ORDER_STATUSES
            and (_dt(row.get("paid_at")) or window.end) < window.end
            and row["id"] not in grant_by_order
        )
        grants_for_unpaid = sum(
            1
            for row in grants
            if not any(
                order["id"] == row["payment_order_id"]
                and str(order.get("status")) in PAID_ORDER_STATUSES
                and order.get("paid_at")
                for order in orders
            )
        )
        payment_window_statuses = Counter(str(row["status"]) for row in orders_window)
        payments = {
            "resulturl_confirmed_payments": len(result_events),
            "orders_created_by_status": dict(sorted(payment_window_statuses.items())),
            "pending_orders": payment_window_statuses.get("pending", 0),
            "expired_orders": payment_window_statuses.get("expired", 0),
            "resulturl_to_grant_latency_seconds": {
                "median": round(float(median(result_to_grant)), 3) if result_to_grant else None,
                "max": round(max(result_to_grant), 3) if result_to_grant else None,
                "samples": len(result_to_grant),
            },
            "idempotency_anomalies": {
                "duplicate_grants": duplicate_grants,
                "duplicate_processed_resulturl_events": duplicate_result_events,
                "paid_orders_missing_grant": paid_missing_grant,
                "grants_for_unpaid_orders": grants_for_unpaid,
            },
            "rejected_callbacks": sum(
                1
                for row in events
                if row.get("event_type") == "result_url"
                and row.get("status") == "rejected"
                and window.contains(_dt(row.get("received_at")))
            ),
            "checkout_transport_attempts": {
                "count": sum(
                    1
                    for row in payment_attempts
                    if row.get("purpose") in {"create_link", "max_checkout_card"}
                    and window.contains(_dt(row.get("started_at")))
                ),
                "note": "Ravuna checkout-generation attempts, not bank/acquiring attempts",
            },
        }

        paid_users_as_of_end = {
            user_id
            for user_id, rows in purchases_as_of_end.items()
            if rows
        }
        grants_window = [row for row in grants if window.contains(_dt(row.get("created_at")))]
        paid_lot_ids = {
            str(row["credit_lot_id"])
            for row in grants
            if (_dt(row.get("created_at")) or window.end) < window.end
        }
        consumed_window = sum(
            1
            for row in reservations
            if row.get("status") == "consumed"
            and str(row.get("lot_id")) in paid_lot_ids
            and window.contains(_dt(row.get("consumed_at")))
        )
        originals_consumed_window = sum(
            1
            for row in entitlements
            if window.contains(_dt(row.get("consumed_at")))
        )
        latest_balance: dict[str, tuple[datetime, int]] = {}
        for row in credit_ledger:
            created = _dt(row.get("created_at"))
            user_id = str(row["user_id"])
            if created and created < window.end and user_id in paid_users_as_of_end:
                current = latest_balance.get(user_id)
                if current is None or created > current[0]:
                    latest_balance[user_id] = (created, int(row["balance_after"]))
        available_originals_by_user: Counter[str] = Counter()
        for row in entitlements:
            created = _dt(row.get("created_at"))
            consumed = _dt(row.get("consumed_at"))
            if (
                created
                and created < window.end
                and str(row["user_id"]) in paid_users_as_of_end
                and (consumed is None or consumed >= window.end)
                and str(row.get("status")) not in {"cancelled", "refunded"}
            ):
                available_originals_by_user[str(row["user_id"])] += 1
        edit_balances = [balance for _, balance in latest_balance.values()]
        original_balances = [available_originals_by_user.get(user_id, 0) for user_id in paid_users_as_of_end]
        entitlements_report = {
            "purchased_edits": sum(int(row["generation_credit_quantity"]) for row in grants_window),
            "consumed_purchased_edits": consumed_window,
            "purchased_originals": sum(int(row["unlock_entitlement_quantity"]) for row in grants_window),
            "consumed_originals": originals_consumed_window,
            "paying_user_balances_as_of_end": {
                "users": len(paid_users_as_of_end),
                "edits_total": sum(edit_balances),
                "edits_median": round(float(median(edit_balances)), 2) if edit_balances else None,
                "originals_total": sum(original_balances),
                "originals_median": round(float(median(original_balances)), 2) if original_balances else None,
            },
            "historical_balance_caveat": (
                "credit balances use the event ledger; original balances use entitlement timestamps/status"
            ),
        }

        jtbd_attempts = [row for row in success_window if str(row["user_id"]) in paid_users_as_of_end]
        jtbd_counts: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"successful_edits": 0, "users": Counter()}
        )
        for row in jtbd_attempts:
            cluster = classify_prompt(str(row.get("prompt") or ""))
            jtbd_counts[cluster]["successful_edits"] += 1
            jtbd_counts[cluster]["users"][str(row["user_id"])] += 1
        jtbd = {
            "clusters": {
                cluster: {
                    "unique_paying_users": len(values["users"]),
                    "successful_edits": values["successful_edits"],
                    "repeat_users": sum(1 for count in values["users"].values() if count >= 2),
                }
                for cluster, values in sorted(jtbd_counts.items())
            },
            "classified_edits": len(jtbd_attempts) - jtbd_counts.get("other", {}).get("successful_edits", 0),
            "total_edits": len(jtbd_attempts),
            "classifier_coverage_percent": _percent(
                len(jtbd_attempts) - jtbd_counts.get("other", {}).get("successful_edits", 0),
                len(jtbd_attempts),
            ),
            "other_visible": True,
        }

        success_counts_all = Counter(
            {
                user_id: sum(1 for stamp in stamps if stamp < window.end)
                for user_id, stamps in success_by_user.items()
            }
        )
        distinct_days_all = {
            user_id: len({stamp.astimezone(zone).date() for stamp in stamps if stamp < window.end})
            for user_id, stamps in success_by_user.items()
        }
        successful_checkout_orders = {
            str(row["order_id"])
            for row in payment_attempts
            if row.get("status") == "succeeded"
            and row.get("purpose") in {"create_link", "max_checkout_card"}
        }
        checkout_non_payers = {
            str(row["user_id"])
            for row in orders_window
            if str(row["id"]) in successful_checkout_orders
            if str(row["user_id"]) not in paid_users_as_of_end
            and success_counts_all.get(str(row["user_id"]), 0) > 0
        }
        first_time_no_return = {
            user_id
            for user_id in first_time_payers
            if not any(
                success > first_paid_at[user_id]
                for success in success_by_user.get(user_id, [])
            )
        }
        repeat_buyers = {
            user_id
            for user_id, rows in purchases_as_of_end.items()
            if len(rows) >= 2
        }
        high_usage_non_payers = {
            user_id for user_id, count in success_counts_all.items() if count >= 5 and user_id not in paid_users_as_of_end
        }
        recurring = {
            user_id
            for user_id, count in success_counts_all.items()
            if count >= 2 and distinct_days_all.get(user_id, 0) >= 2 and user_id != historic_heavy_user
        }

        def cohort(
            values: set[str],
            reason: str,
            *,
            rank: Callable[[str], Any] | None = None,
        ) -> dict[str, Any]:
            ordered = sorted(values, key=rank or masked_id)
            return {
                "count": len(values),
                "masked_ids": [masked_id(value) for value in ordered[:20]],
                "reason": reason,
            }

        candidates = {
            "top_recurring_work_excluding_historic_heavy": cohort(
                recurring,
                "at least 2 successful edits across at least 2 distinct days; historic top payer excluded",
                rank=lambda user_id: (-success_counts_all[user_id], -distinct_days_all.get(user_id, 0), masked_id(user_id)),
            ),
            "successful_checkout_non_payers": cohort(
                checkout_non_payers,
                "successful checkout-link creation and a successful edit, but no confirmed grant by window end",
            ),
            "first_time_payers_never_returned": cohort(
                first_time_no_return,
                "first confirmed payment in window and no later successful edit by window end",
            ),
            "repeat_payers": cohort(
                repeat_buyers,
                "at least 2 ResultURL-confirmed purchases by window end",
                rank=lambda user_id: (-len(purchases_as_of_end[user_id]), masked_id(user_id)),
            ),
            "high_usage_non_payers": cohort(
                high_usage_non_payers,
                "at least 5 successful edits by window end and no confirmed purchase",
                rank=lambda user_id: (-success_counts_all[user_id], masked_id(user_id)),
            ),
        }

        actual_cost_rows = [
            row for row in success_window if row.get("provider_usage_json")
        ]
        economics = {
            "actual_provider_cost": NOT_MEASURABLE,
            "reason": (
                "no authoritative provider billing cost is stored"
                if not actual_cost_rows
                else "provider usage payloads do not establish actual billed currency cost"
            ),
        }

        attribution = self._attribution(
            data.get("attribution_profiles", []), paid_window, users_window, window
        )
        return {
            "label": window.label,
            "from": window.start.isoformat(),
            "to": window.end.isoformat(),
            "timezone": window.timezone_name,
            "funnel": funnel,
            "revenue": revenue,
            "retention": retention,
            "usage_reliability": usage,
            "payments": payments,
            "entitlements": entitlements_report,
            "jtbd": jtbd,
            "interview_candidates": candidates,
            "attribution": attribution,
            "economics": economics,
            "methodology": {
                "window_semantics": "half-open [from,to), timestamps normalized to UTC",
                "funnel_semantics": "unique actors observed at each stage inside the window; conversions use set intersection",
                "cohort_aging": "7/14/30-day denominators exclude cohorts not fully aged at report end",
                "confirmed_payment": "paid order with exactly linked continuation_pack_grant",
            },
        }

    @staticmethod
    def _attribution(
        profiles: list[dict[str, Any]],
        paid_window: list[dict[str, Any]],
        users_window: set[str],
        window: AuditWindow,
    ) -> dict[str, Any]:
        by_user = {str(row["user_id"]): row for row in profiles if row.get("user_id")}
        def source_bucket(value: Any) -> str:
            normalized = str(value or "").casefold()
            if normalized in {"", "unknown", "none"}:
                return "unknown"
            if "refer" in normalized or "share" in normalized:
                return "referral"
            if "manual" in normalized or "outreach" in normalized:
                return "manual_outreach"
            if normalized == "direct":
                return "direct"
            return "other_known"

        source_users: dict[str, set[str]] = defaultdict(set)
        source_payers: dict[str, set[str]] = defaultdict(set)
        source_revenue: Counter[str] = Counter()
        for user_id in users_window:
            source = source_bucket(by_user.get(user_id, {}).get("first_source"))
            source_users[source].add(user_id)
        for row in paid_window:
            user_id = str(row["user_id"])
            source = source_bucket(by_user.get(user_id, {}).get("first_source"))
            source_payers[source].add(user_id)
            source_revenue[source] += int(row["amount_minor"])
        sources = sorted(set(source_users) | set(source_payers))
        return {
            source: {
                "new_users": len(source_users[source]),
                "unique_payers": len(source_payers[source]),
                "payer_conversion_percent": _percent(
                    len(source_users[source] & source_payers[source]), len(source_users[source])
                ),
                "confirmed_revenue_rub": round(source_revenue[source] / 100, 2),
            }
            for source in sources
        }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the exact JSON calculations as compact human-readable Markdown."""
    lines = ["# Ravuna Commercial Intelligence", ""]
    source = report["source"]
    lines.extend(
        [
            f"Source: `{source['database']}` (schema {source['schema_version']}, read-only).",
            "",
        ]
    )
    for window in report["windows"]:
        lines.extend([f"## {window['label']}", ""])
        funnel = window["funnel"]
        lines.append(
            "**Funnel:** "
            + ", ".join(
                f"{key.replace('_', ' ')} {funnel[key]}"
                for key in (
                    "new_users",
                    "uploaded_users",
                    "edit_users",
                    "successful_result_users",
                    "checkout_users",
                    "unique_payers",
                )
            )
            + "."
        )
        revenue = window["revenue"]
        lines.append(
            f"**Revenue:** {revenue['confirmed_revenue_rub']:.2f} ₽; "
            f"{revenue['confirmed_orders']} orders; ARPPU {revenue['arppu_rub']} ₽; "
            f"top-1/top-3 {revenue['top_1_concentration_percent']}%/{revenue['top_3_concentration_percent']}%."
        )
        package_text = ", ".join(
            f"{name}: {values['orders']} orders/{values['unique_buyers']} buyers/{values['revenue_rub']:.2f} ₽"
            for name, values in revenue["packages"].items()
        ) or "none"
        lines.append(f"**Packages:** {package_text}.")
        retention = window["retention"]
        lines.append(
            "**Retention:** first→second success "
            f"{retention['first_success_to_second_success']['second_success_users']}/"
            f"{retention['first_success_to_second_success']['cohort_users']}; "
            f"median second success {retention['median_seconds_to_second_success']}s."
        )
        usage = window["usage_reliability"]
        lines.append(
            f"**Usage/reliability:** {usage['attempts']} attempts, {usage['successes']} successes, "
            f"{usage['failures']} failures; median/p90 {usage['successful_latency_ms']['median']}/"
            f"{usage['successful_latency_ms']['p90']} ms."
        )
        payments = window["payments"]
        latency = payments["resulturl_to_grant_latency_seconds"]
        lines.append(
            f"**Payments:** {payments['resulturl_confirmed_payments']} ResultURL confirmations; "
            f"ResultURL→grant median/max {latency['median']}/{latency['max']}s; "
            f"rejected callbacks {payments['rejected_callbacks']}."
        )
        entitlements = window["entitlements"]
        lines.append(
            f"**Entitlements:** purchased {entitlements['purchased_edits']} edits / "
            f"{entitlements['purchased_originals']} originals; consumed "
            f"{entitlements['consumed_purchased_edits']} / {entitlements['consumed_originals']}."
        )
        clusters = window["jtbd"]["clusters"]
        cluster_text = ", ".join(
            f"{name}={values['successful_edits']}"
            for name, values in sorted(
                clusters.items(), key=lambda item: (-item[1]["successful_edits"], item[0])
            )
        ) or "none"
        lines.append(
            f"**JTBD:** {cluster_text}; coverage {window['jtbd']['classifier_coverage_percent']}%."
        )
        lines.append("**Interview cohorts:**")
        for name, cohort in window["interview_candidates"].items():
            ids = ", ".join(cohort["masked_ids"]) or "none"
            lines.append(f"- {name}: {cohort['count']} ({ids}) — {cohort['reason']}.")
        lines.extend(
            [
                f"**Economics:** {window['economics']['actual_provider_cost']} — {window['economics']['reason']}.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
