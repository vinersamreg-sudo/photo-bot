"""Privacy-safe VK section of the weekly dashboard; both databases read-only."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.database import ReadOnlyDatabase
from app.feedback_report import ReportUnavailable, _owners, _time

NM = "NOT MEASURABLE"


def build_vk_report(settings, repository, *, days: int = 7, at: datetime | None = None) -> dict:
    if not 1 <= days <= 90:
        raise ValueError("VK report days must be between 1 and 90")
    end = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(days=days)
    result = {
        "from": start.isoformat(), "to": end.isoformat(), "timezone": "Europe/Samara",
        "attribution": "first-touch campaign; purchases counted by confirmed unique invoice",
        "engagement_basis": "latest cumulative snapshot, not period increments",
        "database_access": "mode=ro; query_only=ON",
        "posts": [], "limitations": ["Direct MAX links do not measure browser clicks.",
            "Revenue is confirmed gross, not net; refunds/costs are not inferred.",
            "Only events after a recorded first campaign touch and publication are attributed.",
            "Ordinary vertical videos are not confirmed VK Clips."],
    }
    with ReadOnlyDatabase(settings.database_path).read() as connection:
        posts = [dict(row) for row in connection.execute(
            """SELECT p.id,p.source_code,p.published_time AS published_at,p.published_external_id,p.publish_status
               FROM demo_posts p
               WHERE p.platform='vk' ORDER BY p.id"""
        )]
        metrics = {}
        for row in connection.execute("SELECT post_id,metrics_json FROM content_platform_metrics ORDER BY id"):
            metrics[row["post_id"]] = json.loads(row["metrics_json"])
    financial = {}
    try:
        with ReadOnlyDatabase(settings.production_database_path).read() as connection:
            owners = _owners(connection, settings.excluded_max_user_ids)
            financial = _funnel(connection, posts, owners, start, end)
            _bonuses(connection, settings.base_dir / "vk_rewards.sqlite3", posts, owners, financial, start, end)
        result["owner_test_exclusion"] = "verified MAX_OWNER_USER_IDS mapping"
    except (ReportUnavailable, sqlite3.Error, OSError):
        result["owner_test_exclusion"] = "UNAVAILABLE: commercial metrics fail closed"
        result["limitations"].append("Configure verified owner/test identities and provide a compatible read-only ledger.")
    for post in posts:
        published = _time(post["published_at"])
        if not published or published >= end:
            continue
        measured = metrics.get(post["id"], {})
        row = {
            "post": post["id"], "external_id": post["published_external_id"],
            "published_at": published.astimezone(timezone(timedelta(hours=4))).isoformat(),
            "kind": "vertical_video" if post["id"].startswith(("vk-video-", "vk-clip-")) else "photo",
            **{name: measured.get(name, NM) for name in (
                "views", "reactions", "comments", "reach", "impressions", "clicks", "video_views")},
            **financial.get(post["id"], {key: NM for key in (
                "new_max_users", "first_success_users", "first_purchases", "repeat_purchases", "confirmed_gross_revenue_minor")}),
        }
        for key in ("verified_subscriptions", "bonus_granted", "bonus_consumed"):
            row[key] = financial.get(post["id"], {}).get(key, NM)
        result["posts"].append(row)
    result["subscription_bonus"] = "private verified receipts only; not inferred from referrals or membership totals"
    return result


def _bonuses(connection, path, posts, owners, output, start, end):
    if not path.is_file() or path.is_symlink():
        return
    campaigns = {str(p["source_code"])[4:]: p["id"] for p in posts if p["source_code"]}
    users = {row["user_id"]: campaigns[row["first_campaign"]] for row in connection.execute(
        "SELECT user_id,first_campaign FROM attribution_profiles")
        if row["user_id"] not in owners and row["first_campaign"] in campaigns}
    try:
        with ReadOnlyDatabase(path).read() as rewards:
            rows = rewards.execute("SELECT user_id,verified_at,granted_at,state,receipt_json FROM vk_subscription_rewards").fetchall()
        for post in output.values():
            post.update(verified_subscriptions=0, bonus_granted=0, bonus_consumed=0)
        for row in rows:
            if row["user_id"] not in users:
                continue
            post = output[users[row["user_id"]]]
            verified = datetime.fromtimestamp(row["verified_at"], timezone.utc)
            if start <= verified < end:
                post["verified_subscriptions"] += 1
            granted = datetime.fromtimestamp(row["granted_at"], timezone.utc) if row["granted_at"] else None
            if row["state"] == "granted" and granted and granted < end:
                receipt = json.loads(row["receipt_json"] or "{}")
                audit = connection.execute("""SELECT 1 FROM commerce_admin_audit WHERE idempotency_key=?
                    AND reason='vk_subscription_verified' AND delta=1 AND applied=1""", (receipt.get("ledger_key"),)).fetchone()
                if not audit:
                    post["bonus_granted"] = post["bonus_consumed"] = NM
                    continue
                if isinstance(post["bonus_granted"], int):
                    post["bonus_granted"] += int(start <= granted)
                lot = receipt.get("credit_lot")
                if not lot:
                    post["bonus_consumed"] = NM
                    continue
                # Credit-lot provenance is explicit. Count consumed reservations
                # in the report window, not the account's total consumption.
                consumed = connection.execute("""SELECT consumed_at FROM generation_credit_reservations
                    WHERE lot_id=? AND user_id=? AND status='consumed'""", (lot, row["user_id"])).fetchall()
                if isinstance(post["bonus_consumed"], int):
                    post["bonus_consumed"] += sum(start <= _time(value[0]) < end for value in consumed if _time(value[0]))
    except (sqlite3.Error, ValueError, TypeError):
        for post in output.values():
            post.update(verified_subscriptions=NM, bonus_granted=NM, bonus_consumed=NM)


def _funnel(connection, posts, owners, start, end):
    # Native UTC timestamps are parsed, not compared as mixed-offset strings.
    campaigns = {str(p["source_code"])[4:]: p["id"] for p in posts if p["source_code"]}
    users = {}
    arrived = {}
    touched = {}
    published = {p["id"]: _time(p["published_at"]) for p in posts}
    for row in connection.execute("""SELECT a.user_id,a.first_source,a.first_campaign,a.first_started_at,u.created_at
               FROM attribution_profiles a JOIN users u ON u.id=a.user_id"""):
        if row["user_id"] in owners or row["first_source"] not in {"vk", "campaign"} or row["first_campaign"] not in campaigns:
            continue
        user = row["user_id"]
        if not user:
            continue
        post = campaigns[row["first_campaign"]]
        touch = _time(row["first_started_at"])
        if not touch or not published[post] or not published[post] <= touch < end:
            continue
        users[user] = post
        touched[user] = touch
        arrived[user] = _time(row["created_at"])
    first_success = {}
    for row in connection.execute("SELECT user_id,completed_at FROM generation_attempts WHERE status='succeeded'"):
        done = _time(row["completed_at"])
        if done and done < end:
            user = row["user_id"]
            first_success[user] = min(first_success.get(user, done), done)
    payments = {}
    for row in connection.execute("""
        SELECT o.id,o.user_id,o.amount_minor,o.paid_at,e.received_at,e.processed_at,g.created_at AS granted
        FROM payment_orders o JOIN payment_events e ON e.order_id=o.id
        JOIN payment_webhooks w ON w.event_id=e.id
        JOIN continuation_pack_grants g ON g.payment_order_id=o.id AND g.user_id=o.user_id
        WHERE o.provider='robokassa' AND e.provider='robokassa'
          AND e.event_type='result_url' AND e.status='processed'
          AND w.signature_valid=1 AND w.http_status=200 AND o.currency='RUB'
    """):
        if row["user_id"] in owners:
            continue
        times = [_time(row[name]) for name in ("paid_at", "received_at", "processed_at", "granted")]
        if not all(times):
            continue
        paid = max(times)
        if paid >= end:
            continue
        previous = payments.get(row["id"])
        if previous is None or paid < previous[2]:
            payments[row["id"]] = (row["user_id"], row["amount_minor"], paid)
    first_payment = {}
    for invoice, (user, _, paid) in payments.items():
        first_payment[user] = min(first_payment.get(user, (paid, invoice)), (paid, invoice))
    output = {post["id"]: dict.fromkeys(("new_max_users", "first_success_users", "first_purchases",
              "repeat_purchases", "confirmed_gross_revenue_minor"), 0) for post in posts}
    for user, post in users.items():
        if arrived[user] and start <= arrived[user] < end:
            output[post]["new_max_users"] += 1
        if user in first_success and max(start, touched[user]) <= first_success[user] < end:
            output[post]["first_success_users"] += 1
    for invoice, (user, amount, paid) in payments.items():
        if user in users and max(start, touched[user]) <= paid < end:
            row = output[users[user]]
            row["first_purchases" if (paid, invoice) == first_payment[user] else "repeat_purchases"] += 1
            row["confirmed_gross_revenue_minor"] += amount
    return output
