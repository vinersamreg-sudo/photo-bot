"""Opaque referral codes and idempotent bonus-credit rewards."""

from __future__ import annotations

import secrets
import sqlite3
import hashlib
import logging
import re
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.commerce import CommerceService
from app.database import Database


REFERRAL_REWARD_CREDITS = 2
REFERRAL_V2_REWARD_CREDITS = 4
REFERRAL_V2_REWARD_ORIGINALS = 2
REFERRAL_BURST_ALERT_COUNT = 10  # Observation only, NEVER an invitation cap.
REFERRAL_FREE_COST_REVIEW_THRESHOLD_RUB = 5.0  # Existing numeric warning threshold, NOT USD or a spending cap.
LOGGER = logging.getLogger(__name__)


def estimated_cost_rub(value):
    """DemoService persists ProviderResult.estimated_cost_rub or its RUB fallback.

    This is an internal estimate, not provider billing or a currency conversion.
    Missing, malformed, negative or non-finite historical values stay unknown.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if not isinstance(value, bool) and math.isfinite(number) and number >= 0 else None


@dataclass(frozen=True)
class ReferralReward:
    inviter_user_id: str
    inviter_platform_user_id: str
    relationship_id: str
    quantity: int = REFERRAL_REWARD_CREDITS
    originals: int = 0
    program_version: int = 1


class ReferralService:
    def __init__(self, database: Database, commerce: CommerceService, *,
                 v2_enabled: bool = False, promo_grants_enabled: bool = True,
                 clock=lambda: datetime.now(timezone.utc)) -> None:
        self.database = database
        self.commerce = commerce
        self.v2_enabled = v2_enabled
        self.promo_grants_enabled = promo_grants_enabled
        self.clock = clock
        with database.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO referral_program_metadata VALUES('activity_start',?)",
                               (self.clock().isoformat(),))
        if v2_enabled and promo_grants_enabled:
            with database.transaction() as connection:
                connection.execute("INSERT OR IGNORE INTO referral_program_metadata VALUES('v2_activation',?)",
                                   (self.clock().isoformat(),))
            self.reconcile_pending_rewards()

    def reconcile_pending_rewards(self):
        """Recover only V2 rewards with durable confirmed-delivery debit proof."""
        if not self.v2_enabled or not self.promo_grants_enabled:
            return
        with self.database.read() as connection:
            users = [row[0] for row in connection.execute("""SELECT DISTINCT r.invitee_user_id
                FROM referral_relationships r JOIN generation_attempts a ON a.user_id=r.invitee_user_id
                JOIN generation_credit_reservations c ON c.attempt_id=a.id AND c.status='consumed'
                WHERE r.program_version=2 AND r.status='pending' AND a.status='succeeded'""")]
        for user_id in users:
            try:
                self.reward_first_success(user_id)
            except Exception as error:
                LOGGER.error("Referral recovery remains pending (error_type=%s)", type(error).__name__)

    def record_activity(self, platform_user_id, event_key):
        """Authenticated input receipt only; no content, prompt or raw event ID."""
        with self.database.transaction() as connection:
            user = connection.execute("""SELECT u.id FROM users u JOIN referral_relationships r
                ON r.invitee_user_id=u.id WHERE u.platform='max' AND u.platform_user_id=?
                AND r.program_version=2""", (platform_user_id,)).fetchone()
            if user:
                connection.execute("INSERT OR IGNORE INTO referral_activity VALUES(?,?,?)",
                    (hashlib.sha256(event_key.encode()).hexdigest(), user[0], self.clock().isoformat()))

    def observe_free_edit_cost(self, platform_user_id):
        """Warn from observed cost estimates; never fabricate pricing or cap users."""
        now = self.clock().isoformat()
        with self.database.transaction() as connection:
            estimates = [row[0] for row in connection.execute("""SELECT a.estimated_cost
                FROM generation_credit_reservations c JOIN generation_attempts a ON a.id=c.attempt_id
                JOIN generation_credit_lots l ON l.id=c.lot_id WHERE c.status='consumed' AND a.status='succeeded'
                  AND julianday(c.consumed_at)>=julianday(?)-1 AND julianday(c.consumed_at)<=julianday(?)
                  AND (EXISTS(SELECT 1 FROM referral_bonus_grants b WHERE b.credit_lot_id=l.id)
                       OR (l.source_type='initial_free' AND EXISTS(SELECT 1 FROM referral_relationships r
                           WHERE r.invitee_user_id=a.user_id AND r.program_version=2)))""", (now, now))]
            total = sum(value for raw in estimates if (value := estimated_cost_rub(raw)) is not None)
            if math.isfinite(total) and total >= REFERRAL_FREE_COST_REVIEW_THRESHOLD_RUB:
                key = f"referral-estimated-cost:{now[:10]}"
                exists = connection.execute("SELECT 1 FROM referral_diagnostics WHERE event_key=?",
                    (hashlib.sha256(f"referral:{key}:suspected".encode()).hexdigest(),)).fetchone()
                if not exists:
                    self._diagnostic(connection, platform_user_id, key, "suspected", "estimated_free_cost_review_only")
                    LOGGER.warning("Referral free-edit cost review (estimated_rub=%.3f,threshold_rub=%.3f,actual_billing=unavailable); grants not capped", total, REFERRAL_FREE_COST_REVIEW_THRESHOLD_RUB)

    @staticmethod
    def subject(platform_user_id: str) -> str:
        return hashlib.sha256(f"referral-max:{platform_user_id}".encode()).hexdigest()

    def _diagnostic(self, connection, platform_user_id, event_key, kind, reason, *, relationship=None, user_id=None):
        key = hashlib.sha256(f"referral:{event_key}:{kind}".encode()).hexdigest()
        connection.execute("INSERT OR IGNORE INTO referral_diagnostics VALUES(?,?,?,?,?,?,?)",
            (key, relationship, user_id, self.subject(platform_user_id), kind, reason, self.clock().isoformat()))

    def _grant_v2(self, connection, relationship, user_id, beneficiary, bonus_type, quantity):
        key = f"referral-v2:{relationship}:{beneficiary}:{bonus_type}"
        if connection.execute("SELECT 1 FROM referral_bonus_grants WHERE idempotency_key=?", (key,)).fetchone():
            return
        if connection.execute("SELECT 1 FROM commerce_admin_audit WHERE idempotency_key=?", (key,)).fetchone():
            raise RuntimeError("Referral promotion provenance is inconsistent")
        grant_id = uuid4().hex
        reason = "referral_v2_inviter" if beneficiary == "inviter" else "referral_v2_registration"
        lot_id = None
        if bonus_type == "edits":
            before = {row[0] for row in connection.execute("SELECT id FROM generation_credit_lots WHERE user_id=?", (user_id,))}
            self.commerce.adjust_generation_credits(connection, user_id=user_id, delta=quantity,
                reason=reason, idempotency_key=key, referral_relationship_id=relationship)
            created = [row[0] for row in connection.execute("SELECT id FROM generation_credit_lots WHERE user_id=?", (user_id,)) if row[0] not in before]
            if len(created) != 1:
                raise RuntimeError("Referral credit lot was not confirmed")
            lot_id = created[0]
        else:
            self.commerce.adjust_unlock_entitlements(connection, user_id=user_id, delta=quantity,
                                                    reason=reason, idempotency_key=key)
        connection.execute("INSERT INTO referral_bonus_grants VALUES(?,?,?,?,?,?,?,?,?)",
            (grant_id, relationship, user_id, beneficiary, bonus_type, quantity, key, lot_id, self.clock().isoformat()))
        if bonus_type == "originals":
            ids = connection.execute("SELECT id FROM unlock_entitlements WHERE user_id=? AND source_payment_order_id LIKE ?",
                                     (user_id, f"admin:{key}:%")).fetchall()
            if len(ids) != quantity:
                raise RuntimeError("Referral original provenance was not confirmed")
            connection.executemany("INSERT INTO referral_bonus_entitlements(grant_id,entitlement_id) VALUES(?,?)",
                                   [(grant_id, row[0]) for row in ids])

    def get_or_create_code(self, user_id: str) -> str:
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT referral_code FROM referral_codes WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if existing:
                return existing["referral_code"]
            now = datetime.now(timezone.utc).isoformat()
            for _attempt in range(8):
                code = secrets.token_urlsafe(9)
                try:
                    connection.execute(
                        "INSERT INTO referral_codes(user_id,referral_code,created_at) VALUES(?,?,?)",
                        (user_id, code, now),
                    )
                    return code
                except sqlite3.IntegrityError as exc:
                    if "referral_codes.referral_code" not in str(exc):
                        raise
            raise RuntimeError("Could not allocate a unique referral code")

    def register_start(
        self, invitee_platform_user_id: str, referral_code: str, *, event_key: str | None = None
    ) -> Optional[str]:
        now = self.clock().isoformat()
        event_key = event_key or f"registration:{invitee_platform_user_id}:{referral_code}"
        with self.database.transaction() as connection:
            self._diagnostic(connection, invitee_platform_user_id, event_key, "opened", "link_received")
            if not self.promo_grants_enabled:
                self._diagnostic(connection, invitee_platform_user_id, event_key, "paused", "promo_grants_disabled")
                return None
            if not re.fullmatch(r"[A-Za-z0-9_-]{10,20}", referral_code):
                self._diagnostic(connection, invitee_platform_user_id, event_key, "rejected", "invalid_code")
                return None
            existing_user = connection.execute(
                "SELECT id FROM users WHERE platform='max' AND platform_user_id=?",
                (invitee_platform_user_id,),
            ).fetchone()
            code = connection.execute(
                """SELECT c.user_id,u.platform_user_id
                   FROM referral_codes c JOIN users u ON u.id=c.user_id
                   WHERE c.referral_code=? AND u.platform='max'""",
                (referral_code,),
            ).fetchone()
            if (
                code is None
                or code["platform_user_id"] == invitee_platform_user_id
            ):
                self._diagnostic(connection, invitee_platform_user_id, event_key, "rejected",
                    "invalid_code" if code is None else "self_referral",
                    user_id=existing_user["id"] if existing_user else None)
                return None
            existing = connection.execute(
                """SELECT id,referral_code FROM referral_relationships
                   WHERE invitee_platform_user_id=?""",
                (invitee_platform_user_id,),
            ).fetchone()
            if existing:
                self._diagnostic(connection, invitee_platform_user_id, event_key, "duplicate", "first_binding_retained",
                    relationship=existing["id"], user_id=existing_user["id"] if existing_user else None)
                return existing["id"] if existing["referral_code"] == referral_code else None
            prior_start = connection.execute("SELECT 1 FROM attribution_profiles WHERE platform_user_id=?",
                                             (invitee_platform_user_id,)).fetchone()
            if existing_user is not None or prior_start:
                self._diagnostic(connection, invitee_platform_user_id, event_key, "rejected", "existing_account",
                    user_id=existing_user["id"] if existing_user else None)
                return None
            relationship_id = uuid4().hex
            connection.execute(
                """INSERT INTO referral_relationships(
                       id,referral_code,inviter_user_id,invitee_platform_user_id,
                       status,started_at,created_at,updated_at
                   ) VALUES(?,?,?,?, 'pending',?,?,?)""",
                (
                    relationship_id,
                    referral_code,
                    code["user_id"],
                    invitee_platform_user_id,
                    now,
                    now,
                    now,
                ),
            )
            version = 2 if self.v2_enabled else 1
            connection.execute("UPDATE referral_relationships SET program_version=? WHERE id=?", (version, relationship_id))
            user_id = None
            if version == 2:
                user_id = uuid4().hex
                connection.execute("INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,'max',?,?)",
                                   (user_id, invitee_platform_user_id, now))
                self.commerce.ensure_initial_grant(connection, user_id)
                connection.execute("UPDATE referral_relationships SET invitee_user_id=?,registered_at=? WHERE id=?", (user_id, now, relationship_id))
                self._grant_v2(connection, relationship_id, user_id, "invitee", "originals", 1)
            self._diagnostic(connection, invitee_platform_user_id, event_key, "accepted", f"v{version}_registration",
                             relationship=relationship_id, user_id=user_id)
            recent = connection.execute("SELECT COUNT(*) FROM referral_relationships WHERE inviter_user_id=? AND julianday(started_at)>=julianday(?)-1.0/24",
                                        (code["user_id"], now)).fetchone()[0]
            if recent >= REFERRAL_BURST_ALERT_COUNT:
                self._diagnostic(connection, invitee_platform_user_id, f"burst:{code['user_id']}:{now[:13]}",
                                 "suspected", "rapid_registrations_review_only", relationship=relationship_id, user_id=user_id)
                LOGGER.warning("Referral activity review suggested (reason=rapid_registrations,count=%s); no grant cap applied", recent)
            return relationship_id

    def inviter_for_relationship(self, relationship_id: Optional[str]) -> Optional[str]:
        if not relationship_id:
            return None
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT inviter_user_id FROM referral_relationships WHERE id=?",
                (relationship_id,),
            ).fetchone()
        return row["inviter_user_id"] if row else None

    def link_user(self, invitee_platform_user_id: str, invitee_user_id: str) -> None:
        now = self.clock().isoformat()
        with self.database.transaction() as connection:
            relationship = connection.execute(
                """SELECT * FROM referral_relationships
                   WHERE invitee_platform_user_id=?""",
                (invitee_platform_user_id,),
            ).fetchone()
            if relationship is None or relationship["status"] != "pending":
                return
            identity = connection.execute("SELECT platform,platform_user_id FROM users WHERE id=?", (invitee_user_id,)).fetchone()
            if not identity or identity["platform"] != "max" or identity["platform_user_id"] != invitee_platform_user_id:
                self._diagnostic(connection, invitee_platform_user_id, f"identity:{relationship['id']}",
                                 "rejected", "identity_mismatch", relationship=relationship["id"])
                return
            if relationship["inviter_user_id"] == invitee_user_id:
                connection.execute(
                    """UPDATE referral_relationships
                       SET status='disqualified',updated_at=? WHERE id=?""",
                    (now, relationship["id"]),
                )
                return
            connection.execute(
                """UPDATE referral_relationships SET invitee_user_id=?,updated_at=?,registered_at=?
                   WHERE id=? AND invitee_user_id IS NULL""",
                (invitee_user_id, now, now, relationship["id"]),
            )

    def reward_first_success(self, invitee_user_id: str) -> Optional[ReferralReward]:
        if not self.promo_grants_enabled:
            return None
        now = self.clock().isoformat()
        with self.database.transaction() as connection:
            relationship = connection.execute(
                """SELECT * FROM referral_relationships
                   WHERE invitee_user_id=? AND status='pending'""",
                (invitee_user_id,),
            ).fetchone()
            if relationship is None:
                return None
            if relationship["program_version"] == 2:
                if not self.v2_enabled:
                    return None
                invitee = connection.execute("SELECT platform,platform_user_id FROM users WHERE id=?", (invitee_user_id,)).fetchone()
                inviter = connection.execute("SELECT platform,platform_user_id FROM users WHERE id=?", (relationship["inviter_user_id"],)).fetchone()
                if (not invitee or not inviter or invitee["platform"] != "max" or inviter["platform"] != "max"
                        or invitee["platform_user_id"] != relationship["invitee_platform_user_id"]
                        or invitee["platform_user_id"] == inviter["platform_user_id"]):
                    return None
                success = connection.execute("""SELECT a.id,COALESCE(c.consumed_at,a.completed_at,a.started_at) AS at
                    FROM generation_attempts a JOIN generation_credit_reservations c ON c.attempt_id=a.id
                    WHERE a.user_id=? AND a.status='succeeded' AND c.status='consumed'
                    ORDER BY a.started_at,a.id LIMIT 1""", (invitee_user_id,)).fetchone()
                if not success:
                    return None
                self._grant_v2(connection, relationship["id"], relationship["inviter_user_id"], "inviter", "edits", 4)
                self._grant_v2(connection, relationship["id"], relationship["inviter_user_id"], "inviter", "originals", 2)
                connection.execute("UPDATE referral_relationships SET status='rewarded',rewarded_at=?,updated_at=?,first_success_at=?,first_success_attempt_id=? WHERE id=?",
                    (now, now, success["at"], success["id"], relationship["id"]))
                self._diagnostic(connection, invitee["platform_user_id"], f"success:{relationship['id']}",
                                 "first_success", "v2_first_success", relationship=relationship["id"], user_id=invitee_user_id)
                return ReferralReward(relationship["inviter_user_id"], inviter["platform_user_id"],
                                      relationship["id"], 4, 2, 2)
            successes = connection.execute(
                """SELECT COUNT(*) AS quantity FROM generation_attempts
                   WHERE user_id=? AND status='succeeded'""",
                (invitee_user_id,),
            ).fetchone()["quantity"]
            if int(successes) != 1:
                if int(successes) > 1:
                    connection.execute(
                        """UPDATE referral_relationships
                           SET status='disqualified',updated_at=? WHERE id=?""",
                        (now, relationship["id"]),
                    )
                return None
            idempotency_key = f"referral-reward:{relationship['id']}"
            self.commerce.adjust_generation_credits(
                connection,
                user_id=relationship["inviter_user_id"],
                delta=REFERRAL_REWARD_CREDITS,
                reason="referral_first_success",
                idempotency_key=idempotency_key,
            )
            connection.execute(
                """INSERT OR IGNORE INTO bonus_credit_transactions(
                       id,relationship_id,user_id,quantity,idempotency_key,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    relationship["id"],
                    relationship["inviter_user_id"],
                    REFERRAL_REWARD_CREDITS,
                    idempotency_key,
                    now,
                ),
            )
            updated = connection.execute(
                """UPDATE referral_relationships SET status='rewarded',rewarded_at=?,updated_at=?
                   WHERE id=? AND status='pending'""",
                (now, now, relationship["id"]),
            ).rowcount
            if not updated:
                return None
            inviter = connection.execute(
                "SELECT platform_user_id FROM users WHERE id=?",
                (relationship["inviter_user_id"],),
            ).fetchone()
            if inviter is None:
                return None
            return ReferralReward(
                relationship["inviter_user_id"],
                inviter["platform_user_id"],
                relationship["id"],
            )
