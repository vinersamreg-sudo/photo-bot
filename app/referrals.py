"""Opaque referral codes and idempotent bonus-credit rewards."""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.commerce import CommerceService
from app.database import Database


REFERRAL_REWARD_CREDITS = 2


@dataclass(frozen=True)
class ReferralReward:
    inviter_user_id: str
    inviter_platform_user_id: str
    relationship_id: str
    quantity: int = REFERRAL_REWARD_CREDITS


class ReferralService:
    def __init__(self, database: Database, commerce: CommerceService) -> None:
        self.database = database
        self.commerce = commerce

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
        self, invitee_platform_user_id: str, referral_code: str
    ) -> Optional[str]:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            existing_user = connection.execute(
                "SELECT id FROM users WHERE platform='max' AND platform_user_id=?",
                (invitee_platform_user_id,),
            ).fetchone()
            code = connection.execute(
                """SELECT c.user_id,u.platform_user_id
                   FROM referral_codes c JOIN users u ON u.id=c.user_id
                   WHERE c.referral_code=?""",
                (referral_code,),
            ).fetchone()
            if (
                code is None
                or existing_user is not None
                or code["platform_user_id"] == invitee_platform_user_id
            ):
                return None
            existing = connection.execute(
                """SELECT id FROM referral_relationships
                   WHERE invitee_platform_user_id=?""",
                (invitee_platform_user_id,),
            ).fetchone()
            if existing:
                return existing["id"]
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
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            relationship = connection.execute(
                """SELECT * FROM referral_relationships
                   WHERE invitee_platform_user_id=?""",
                (invitee_platform_user_id,),
            ).fetchone()
            if relationship is None or relationship["status"] != "pending":
                return
            if relationship["inviter_user_id"] == invitee_user_id:
                connection.execute(
                    """UPDATE referral_relationships
                       SET status='disqualified',updated_at=? WHERE id=?""",
                    (now, relationship["id"]),
                )
                return
            connection.execute(
                """UPDATE referral_relationships SET invitee_user_id=?,updated_at=?
                   WHERE id=? AND invitee_user_id IS NULL""",
                (invitee_user_id, now, relationship["id"]),
            )

    def reward_first_success(self, invitee_user_id: str) -> Optional[ReferralReward]:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            relationship = connection.execute(
                """SELECT * FROM referral_relationships
                   WHERE invitee_user_id=? AND status='pending'""",
                (invitee_user_id,),
            ).fetchone()
            if relationship is None:
                return None
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
