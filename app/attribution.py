"""Privacy-bounded first-touch attribution for MAX start payloads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import Database


ATTRIBUTION_EVENTS = {
    "bot_started",
    "photo_uploaded",
    "first_generation_success",
    "payment_offer_opened",
    "payment_started",
    "payment_success",
    "share_opened",
    "referral_started",
    "referral_rewarded",
}
_CODE = r"[a-z0-9][a-z0-9-]{0,31}"
_REFERRAL = re.compile(r"^ref_([A-Za-z0-9_-]{10,20})$")
_PARTNER = re.compile(rf"^src_partner_({_CODE})$")
_CAMPAIGN = re.compile(rf"^src_({_CODE})$")


@dataclass(frozen=True)
class StartAttribution:
    source: str
    campaign: Optional[str] = None
    referral_code: Optional[str] = None


def parse_start_payload(payload: Optional[str]) -> StartAttribution:
    value = (payload or "").strip()
    if not value:
        return StartAttribution("direct")
    referral = _REFERRAL.fullmatch(value)
    if referral:
        return StartAttribution("referral", referral.group(1), referral.group(1))
    known = {
        "src_max_channel": "max_channel",
        "src_site": "site",
        "src_vk": "vk",
        "src_ok": "ok",
    }
    if value in known:
        return StartAttribution(known[value])
    partner = _PARTNER.fullmatch(value)
    if partner:
        return StartAttribution("partner", partner.group(1))
    campaign = _CAMPAIGN.fullmatch(value)
    if campaign and campaign.group(1) not in {"partner"}:
        return StartAttribution("campaign", campaign.group(1))
    return StartAttribution("direct")


class AttributionService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record_start(
        self,
        platform_user_id: str,
        payload: Optional[str],
        *,
        event_key: str,
        first_referrer_user_id: Optional[str] = None,
    ) -> StartAttribution:
        attribution = parse_start_payload(payload)
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            user = connection.execute(
                "SELECT id FROM users WHERE platform='max' AND platform_user_id=?",
                (platform_user_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO attribution_profiles(
                       platform_user_id,user_id,first_source,first_campaign,
                       first_referrer_user_id,first_started_at,last_source,last_campaign,
                       last_started_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform_user_id) DO UPDATE SET
                       user_id=COALESCE(attribution_profiles.user_id,excluded.user_id),
                       last_source=excluded.last_source,
                       last_campaign=excluded.last_campaign,
                       last_started_at=excluded.last_started_at,
                       updated_at=excluded.updated_at""",
                (
                    platform_user_id,
                    user["id"] if user else None,
                    attribution.source,
                    attribution.campaign,
                    first_referrer_user_id,
                    now,
                    attribution.source,
                    attribution.campaign,
                    now,
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                platform_user_id,
                user["id"] if user else None,
                "bot_started",
                event_key,
                attribution.source,
                attribution.campaign,
                now,
            )
        return attribution

    def link_user(self, platform_user_id: str, user_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE attribution_profiles SET user_id=COALESCE(user_id,?),updated_at=?
                   WHERE platform_user_id=?""",
                (user_id, datetime.now(timezone.utc).isoformat(), platform_user_id),
            )
            connection.execute(
                """UPDATE attribution_events SET user_id=COALESCE(user_id,?)
                   WHERE platform_user_id=?""",
                (user_id, platform_user_id),
            )

    def record_event(
        self,
        platform_user_id: str,
        event_type: str,
        *,
        idempotency_key: str,
        user_id: Optional[str] = None,
    ) -> bool:
        if event_type not in ATTRIBUTION_EVENTS:
            raise ValueError("Unknown attribution event")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            profile = connection.execute(
                "SELECT * FROM attribution_profiles WHERE platform_user_id=?",
                (platform_user_id,),
            ).fetchone()
            source = profile["first_source"] if profile else "direct"
            campaign = profile["first_campaign"] if profile else None
            return bool(
                self._insert_event(
                    connection,
                    platform_user_id,
                    user_id or (profile["user_id"] if profile else None),
                    event_type,
                    idempotency_key,
                    source,
                    campaign,
                    now,
                )
            )

    @staticmethod
    def _insert_event(
        connection,
        platform_user_id: str,
        user_id: Optional[str],
        event_type: str,
        idempotency_key: str,
        source: Optional[str],
        campaign: Optional[str],
        created_at: str,
    ) -> int:
        return connection.execute(
            """INSERT OR IGNORE INTO attribution_events(
                   platform_user_id,user_id,event_type,source,campaign,
                   idempotency_key,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                platform_user_id,
                user_id,
                event_type,
                source,
                campaign,
                idempotency_key,
                created_at,
            ),
        ).rowcount

    def report(self, days: int = 30) -> dict[str, object]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT COALESCE(source,'direct') AS source,
                          SUM(event_type='bot_started') AS starts,
                          SUM(event_type='first_generation_success') AS first_edits,
                          SUM(event_type='payment_success') AS payments
                   FROM attribution_events WHERE created_at>=?
                   GROUP BY COALESCE(source,'direct') ORDER BY starts DESC,source""",
                (since,),
            ).fetchall()
            totals = connection.execute(
                """SELECT SUM(event_type='referral_started') AS referral_links_opened,
                          SUM(event_type='referral_rewarded') AS successful_referrals,
                          (SELECT COUNT(*) FROM bonus_credit_transactions
                           WHERE created_at>=?) AS referral_rewards_issued,
                          SUM(event_type='share_opened') AS share_button_clicks
                   FROM attribution_events WHERE created_at>=?""",
                (since, since),
            ).fetchone()
        sources = [
            {
                "source": row["source"],
                "starts": int(row["starts"] or 0),
                "first_edits": int(row["first_edits"] or 0),
                "payments": int(row["payments"] or 0),
                "conversion": (
                    float(row["payments"] or 0) / float(row["starts"])
                    if row["starts"]
                    else 0.0
                ),
            }
            for row in rows
        ]
        return {
            "days": days,
            "sources": sources,
            "referral_links_opened": int(totals["referral_links_opened"] or 0),
            "successful_referrals": int(totals["successful_referrals"] or 0),
            "referral_rewards_issued": int(totals["referral_rewards_issued"] or 0),
            "share_button_clicks": int(totals["share_button_clicks"] or 0),
        }
