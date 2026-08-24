"""Transactional Ravuna v1 credit, package and original-entitlement ledger."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from app.database import Database
from app.domain import DemoLimitError, InvalidInputError, PaymentRequiredError


@dataclass(frozen=True)
class ContinuationPackage:
    code: str
    product_name: str
    user_name: str
    receipt_name: str
    price_minor: int
    generation_credits: int
    unlock_entitlements: int


SMALL_PACKAGE = ContinuationPackage(
    code="continuation_pack_2_plus_1",
    product_name="Ravuna Access Pack",
    user_name="Пакет доступа Ravuna",
    receipt_name="Пакет доступа Ravuna",
    price_minor=4_900,
    generation_credits=2,
    unlock_entitlements=1,
)
LARGE_PACKAGE = ContinuationPackage(
    code="continuation_pack_100_plus_50",
    product_name="Ravuna 100 + 50 Pack",
    user_name="Пакет Ravuna: 100 обработок и 50 оригиналов",
    receipt_name="Пакет Ravuna: 100 обработок и 50 оригиналов",
    price_minor=199_000,
    generation_credits=100,
    unlock_entitlements=50,
)
CONTINUATION_PACKAGES = {
    package.code: package for package in (SMALL_PACKAGE, LARGE_PACKAGE)
}


def continuation_package(product_code: str) -> ContinuationPackage:
    try:
        return CONTINUATION_PACKAGES[product_code]
    except KeyError as exc:
        raise InvalidInputError("Unknown continuation package") from exc


def is_continuation_package(product_code: str) -> bool:
    return product_code in CONTINUATION_PACKAGES


# Compatibility names for the original public package.
PRODUCT_CODE = SMALL_PACKAGE.code
PRODUCT_NAME = SMALL_PACKAGE.product_name
USER_PRODUCT_NAME = SMALL_PACKAGE.user_name
RECEIPT_ITEM_NAME = SMALL_PACKAGE.receipt_name
PRICE_MINOR = SMALL_PACKAGE.price_minor
GENERATION_CREDITS_PER_PACK = SMALL_PACKAGE.generation_credits
UNLOCK_ENTITLEMENTS_PER_PACK = SMALL_PACKAGE.unlock_entitlements
INITIAL_GENERATION_CREDITS = 2
MAX_ACCOUNT_BALANCE = 1_000_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _subject_hash(user_id: str) -> str:
    return hashlib.sha256(f"pixora-product-subject:{user_id}".encode()).hexdigest()


def _event(
    connection: sqlite3.Connection,
    event_type: str,
    now: str,
    *,
    user_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    gallery_item_id: Optional[str] = None,
    error_type: Optional[str] = None,
    value_integer: Optional[int] = None,
    value_real: Optional[float] = None,
) -> None:
    connection.execute(
        """INSERT INTO product_events(
               event_type,created_at,attempt_id,gallery_item_id,error_type,
               subject_hash,value_integer,value_real
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            event_type,
            now,
            attempt_id,
            gallery_item_id,
            error_type,
            _subject_hash(user_id) if user_id else None,
            value_integer,
            value_real,
        ),
    )


@dataclass(frozen=True)
class CreditBalance:
    available: int
    reserved: int
    total_granted: int
    total_consumed: int
    total_refunded: int


@dataclass(frozen=True)
class EntitlementBalance:
    available: int
    consumed: int
    refunded: int


@dataclass(frozen=True)
class UnlockResult:
    version_id: str
    original_path: Path
    entitlement_id: Optional[str]
    consumed_now: bool


@dataclass(frozen=True)
class UnlockReservation:
    version_id: str
    original_path: Path
    entitlement_id: Optional[str]
    already_unlocked: bool


@dataclass(frozen=True)
class RefundEligibility:
    eligible: bool
    reason: str


def migrate_legacy_credit_accounts(
    connection: sqlite3.Connection,
    now: str,
) -> dict[str, int]:
    """Backfill exactly one lifetime initial grant without re-granting old users."""

    created = 0
    exhausted = 0
    partially_used = 0
    for user in connection.execute("SELECT id FROM users ORDER BY created_at,id").fetchall():
        user_id = user["id"]
        if connection.execute(
            "SELECT 1 FROM user_credit_accounts WHERE user_id=?", (user_id,)
        ).fetchone():
            continue
        historical = int(
            connection.execute(
                """SELECT COUNT(*) FROM generation_attempts
                   WHERE user_id=? AND status='succeeded'""",
                (user_id,),
            ).fetchone()[0]
        )
        consumed = min(INITIAL_GENERATION_CREDITS, historical)
        available = INITIAL_GENERATION_CREDITS - consumed
        lot_id = uuid4().hex
        connection.execute(
            """INSERT INTO user_credit_accounts(
                   user_id,free_grant_applied,available_generation_credits,
                   total_generation_credits_granted,total_generation_credits_consumed,
                   created_at,updated_at
               ) VALUES(?,1,?,?,?,?,?)""",
            (
                user_id,
                available,
                INITIAL_GENERATION_CREDITS,
                consumed,
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO generation_credit_lots(
                   id,user_id,source_type,granted_credits,available_credits,
                   consumed_credits,status,created_at,updated_at
               ) VALUES(?,?,'initial_free',?,?,?,?,?,?)""",
            (
                lot_id,
                user_id,
                INITIAL_GENERATION_CREDITS,
                available,
                consumed,
                "exhausted" if available == 0 else "active",
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO credit_ledger(
                   user_id,delta,event_type,reference_type,reference_id,
                   idempotency_key,balance_after,reserved_after,created_at
               ) VALUES(?,?,'initial_free_grant','credit_lot',?,?,?,0,?)""",
            (
                user_id,
                INITIAL_GENERATION_CREDITS,
                lot_id,
                f"initial-free:{user_id}",
                INITIAL_GENERATION_CREDITS,
                now,
            ),
        )
        if consumed:
            connection.execute(
                """INSERT INTO credit_ledger(
                       user_id,delta,event_type,reference_type,reference_id,
                       idempotency_key,balance_after,reserved_after,created_at
                   ) VALUES(?,?,'legacy_success_backfill','migration',?,?,?,0,?)""",
                (
                    user_id,
                    -consumed,
                    lot_id,
                    f"legacy-credit-backfill:{user_id}",
                    available,
                    now,
                ),
            )
        _event(
            connection,
            "initial_free_pack_granted",
            now,
            user_id=user_id,
            value_integer=INITIAL_GENERATION_CREDITS,
        )
        created += 1
        exhausted += int(available == 0)
        partially_used += int(available == 1)
    return {"created": created, "exhausted": exhausted, "partially_used": partially_used}


class CommerceService:
    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] = _utc_now,
        paid_retention_days: int = 180,
    ) -> None:
        self.database = database
        self.clock = clock
        self.paid_retention_days = paid_retention_days

    @staticmethod
    def _balance_row(row: sqlite3.Row) -> CreditBalance:
        return CreditBalance(
            int(row["available_generation_credits"]),
            int(row["reserved_generation_credits"]),
            int(row["total_generation_credits_granted"]),
            int(row["total_generation_credits_consumed"]),
            int(row["total_generation_credits_refunded"]),
        )

    def ensure_initial_grant(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        *,
        historical_consumed: int = 0,
    ) -> CreditBalance:
        row = connection.execute(
            "SELECT * FROM user_credit_accounts WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            migrate_legacy_credit_accounts(connection, _iso(self.clock()))
            row = connection.execute(
                "SELECT * FROM user_credit_accounts WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            raise InvalidInputError("Credit account could not be initialized")
        return self._balance_row(row)

    def balance(self, user_id: str) -> CreditBalance:
        with self.database.transaction() as connection:
            return self.ensure_initial_grant(connection, user_id)

    def entitlement_balance(self, user_id: str) -> EntitlementBalance:
        with self.database.read() as connection:
            rows = {
                row["status"]: int(row["quantity"])
                for row in connection.execute(
                    """SELECT status,COUNT(*) AS quantity FROM unlock_entitlements
                       WHERE user_id=? GROUP BY status""",
                    (user_id,),
                ).fetchall()
            }
        return EntitlementBalance(
            rows.get("available", 0), rows.get("consumed", 0), rows.get("refunded", 0)
        )

    def adjust_generation_credits(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        delta: int,
        reason: str,
        idempotency_key: str,
    ) -> CreditBalance:
        if delta == 0 or not reason.strip() or not idempotency_key:
            raise InvalidInputError("Non-zero delta, reason and idempotency key are required")
        existing = connection.execute(
            "SELECT * FROM commerce_admin_audit WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if existing["entity_type"] != "generation_credit" or existing["delta"] != delta:
                raise InvalidInputError("Admin adjustment idempotency conflict")
            row = connection.execute(
                "SELECT * FROM user_credit_accounts WHERE user_id=?", (user_id,)
            ).fetchone()
            return self._balance_row(row)
        account = self.ensure_initial_grant(connection, user_id)
        if delta < 0 and account.available < -delta:
            raise InvalidInputError("Adjustment would make generation balance negative")
        if delta > 0 and account.available + account.reserved + delta > MAX_ACCOUNT_BALANCE:
            raise InvalidInputError("Generation balance safety limit exceeded")
        now = _iso(self.clock())
        if delta > 0:
            lot_id = uuid4().hex
            connection.execute(
                """INSERT INTO generation_credit_lots(
                       id,user_id,source_type,granted_credits,available_credits,
                       created_at,updated_at
                   ) VALUES(?,?,'admin',?,?,?,?)""",
                (lot_id, user_id, delta, delta, now, now),
            )
            connection.execute(
                """UPDATE user_credit_accounts SET
                   available_generation_credits=available_generation_credits+?,
                   total_generation_credits_adjusted=total_generation_credits_adjusted+?,
                   updated_at=?,version=version+1 WHERE user_id=?""",
                (delta, delta, now, user_id),
            )
        else:
            remaining = -delta
            lots = connection.execute(
                """SELECT * FROM generation_credit_lots
                   WHERE user_id=? AND status='active' AND available_credits>0
                   ORDER BY created_at,id""",
                (user_id,),
            ).fetchall()
            for lot in lots:
                if remaining == 0:
                    break
                amount = min(remaining, int(lot["available_credits"]))
                connection.execute(
                    """UPDATE generation_credit_lots SET
                       available_credits=available_credits-?,refunded_credits=refunded_credits+?,
                       status=CASE WHEN available_credits=? THEN 'refunded' ELSE status END,
                       updated_at=? WHERE id=?""",
                    (amount, amount, amount, now, lot["id"]),
                )
                remaining -= amount
            if remaining:
                raise InvalidInputError("Credit lot ledger is inconsistent")
            connection.execute(
                """UPDATE user_credit_accounts SET
                   available_generation_credits=available_generation_credits+?,
                   total_generation_credits_adjusted=total_generation_credits_adjusted+?,
                   updated_at=?,version=version+1 WHERE user_id=?""",
                (delta, delta, now, user_id),
            )
        refreshed = connection.execute(
            "SELECT * FROM user_credit_accounts WHERE user_id=?", (user_id,)
        ).fetchone()
        connection.execute(
            """INSERT INTO credit_ledger(
                   user_id,delta,event_type,reference_type,reference_id,idempotency_key,
                   balance_after,reserved_after,created_at
               ) VALUES(? ,?,'admin_adjustment','admin',?,?,?, ?,?)""",
            (
                user_id, delta, idempotency_key, f"credit-adjust:{idempotency_key}",
                refreshed["available_generation_credits"],
                refreshed["reserved_generation_credits"], now,
            ),
        )
        connection.execute(
            """INSERT INTO commerce_admin_audit(
                   subject_hash,entity_type,delta,reason,idempotency_key,applied,created_at
               ) VALUES(?,'generation_credit',?,?,?,1,?)""",
            (_subject_hash(user_id), delta, reason.strip()[:300], idempotency_key, now),
        )
        return self._balance_row(refreshed)

    def adjust_unlock_entitlements(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        delta: int,
        reason: str,
        idempotency_key: str,
    ) -> EntitlementBalance:
        if delta == 0 or not reason.strip() or not idempotency_key:
            raise InvalidInputError("Non-zero delta, reason and idempotency key are required")
        existing = connection.execute(
            "SELECT * FROM commerce_admin_audit WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if existing["entity_type"] != "unlock_entitlement" or existing["delta"] != delta:
                raise InvalidInputError("Admin adjustment idempotency conflict")
            rows = {
                row["status"]: int(row["quantity"])
                for row in connection.execute(
                    """SELECT status,COUNT(*) AS quantity FROM unlock_entitlements
                       WHERE user_id=? GROUP BY status""",
                    (user_id,),
                ).fetchall()
            }
            return EntitlementBalance(
                rows.get("available", 0), rows.get("consumed", 0), rows.get("refunded", 0)
            )
        now = _iso(self.clock())
        if delta > 0:
            for index in range(delta):
                token = f"admin:{idempotency_key}:{index}"
                connection.execute(
                    """INSERT INTO unlock_entitlements(
                           id,user_id,source_payment_order_id,status,created_at,updated_at
                       ) VALUES(?,?,?,'available',?,?)""",
                    (uuid4().hex, user_id, token, now, now),
                )
        else:
            available = connection.execute(
                """SELECT id FROM unlock_entitlements WHERE user_id=? AND status='available'
                   ORDER BY created_at,id LIMIT ?""",
                (user_id, -delta),
            ).fetchall()
            if len(available) != -delta:
                raise InvalidInputError("Adjustment would make entitlement balance negative")
            for row in available:
                connection.execute(
                    """UPDATE unlock_entitlements SET status='cancelled',updated_at=?
                       WHERE id=? AND status='available'""",
                    (now, row["id"]),
                )
        connection.execute(
            """INSERT INTO commerce_admin_audit(
                   subject_hash,entity_type,delta,reason,idempotency_key,applied,created_at
               ) VALUES(?,'unlock_entitlement',?,?,?,1,?)""",
            (_subject_hash(user_id), delta, reason.strip()[:300], idempotency_key, now),
        )
        rows = {
            row["status"]: int(row["quantity"])
            for row in connection.execute(
                """SELECT status,COUNT(*) AS quantity FROM unlock_entitlements
                   WHERE user_id=? GROUP BY status""",
                (user_id,),
            ).fetchall()
        }
        return EntitlementBalance(
            rows.get("available", 0), rows.get("consumed", 0), rows.get("refunded", 0)
        )

    def reserve_generation(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        attempt_id: str,
        idempotency_key: str,
    ) -> str:
        replay = connection.execute(
            "SELECT * FROM generation_credit_reservations WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay:
            if replay["attempt_id"] != attempt_id:
                raise InvalidInputError("Credit reservation idempotency key was reused")
            return replay["id"]
        account = self.ensure_initial_grant(connection, user_id)
        if account.available <= 0:
            raise DemoLimitError("Generation balance is exhausted")
        lot = connection.execute(
            """SELECT * FROM generation_credit_lots
               WHERE user_id=? AND status='active' AND available_credits>0
               ORDER BY created_at,id LIMIT 1""",
            (user_id,),
        ).fetchone()
        if lot is None:
            # A refund hold intentionally removes a paid lot from the spendable
            # set while the durable account totals remain auditable.  Treat this
            # as an unavailable balance, not as corrupt user input.
            raise DemoLimitError("Generation balance is temporarily unavailable")
        now = _iso(self.clock())
        reservation_id = uuid4().hex
        connection.execute(
            """UPDATE user_credit_accounts
               SET available_generation_credits=available_generation_credits-1,
                   reserved_generation_credits=reserved_generation_credits+1,
                   updated_at=?,version=version+1
               WHERE user_id=? AND available_generation_credits>0""",
            (now, user_id),
        )
        connection.execute(
            """UPDATE generation_credit_lots
               SET available_credits=available_credits-1,reserved_credits=reserved_credits+1,
                   updated_at=? WHERE id=?""",
            (now, lot["id"]),
        )
        connection.execute(
            """INSERT INTO generation_credit_reservations(
                   id,user_id,lot_id,attempt_id,idempotency_key,status,reserved_at,updated_at
               ) VALUES(?,?,?,?,?,'reserved',?,?)""",
            (reservation_id, user_id, lot["id"], attempt_id, idempotency_key, now, now),
        )
        connection.execute(
            """INSERT INTO credit_ledger(
                   user_id,delta,event_type,reference_type,reference_id,idempotency_key,
                   balance_after,reserved_after,created_at
               ) VALUES(?,-1,'generation_reserved','attempt',?,?,?, ?,?)""",
            (
                user_id,
                attempt_id,
                f"credit-reserve:{idempotency_key}",
                account.available - 1,
                account.reserved + 1,
                now,
            ),
        )
        _event(
            connection,
            "generation_credit_reserved",
            now,
            user_id=user_id,
            attempt_id=attempt_id,
            value_integer=account.available - 1,
        )
        return reservation_id

    def consume_generation(
        self, connection: sqlite3.Connection, reservation_id: str
    ) -> CreditBalance:
        reservation = connection.execute(
            "SELECT * FROM generation_credit_reservations WHERE id=?", (reservation_id,)
        ).fetchone()
        if reservation is None:
            raise InvalidInputError("Generation reservation is missing")
        if reservation["status"] == "consumed":
            row = connection.execute(
                "SELECT * FROM user_credit_accounts WHERE user_id=?",
                (reservation["user_id"],),
            ).fetchone()
            return self._balance_row(row)
        if reservation["status"] != "reserved":
            raise InvalidInputError("Released generation credit cannot be consumed")
        now = _iso(self.clock())
        connection.execute(
            """UPDATE generation_credit_reservations
               SET status='consumed',consumed_at=?,updated_at=? WHERE id=?""",
            (now, now, reservation_id),
        )
        connection.execute(
            """UPDATE generation_credit_lots
               SET reserved_credits=reserved_credits-1,consumed_credits=consumed_credits+1,
                   status=CASE WHEN available_credits=0 AND reserved_credits=1
                               THEN 'exhausted' ELSE status END,
                   updated_at=? WHERE id=?""",
            (now, reservation["lot_id"]),
        )
        connection.execute(
            """UPDATE user_credit_accounts
               SET reserved_generation_credits=reserved_generation_credits-1,
                   total_generation_credits_consumed=total_generation_credits_consumed+1,
                   updated_at=?,version=version+1 WHERE user_id=?""",
            (now, reservation["user_id"]),
        )
        account = connection.execute(
            "SELECT * FROM user_credit_accounts WHERE user_id=?", (reservation["user_id"],)
        ).fetchone()
        connection.execute(
            """INSERT INTO credit_ledger(
                   user_id,delta,event_type,reference_type,reference_id,idempotency_key,
                   balance_after,reserved_after,created_at
               ) VALUES(?,0,'generation_consumed','attempt',?,?,?, ?,?)""",
            (
                reservation["user_id"],
                reservation["attempt_id"],
                f"credit-consume:{reservation_id}",
                account["available_generation_credits"],
                account["reserved_generation_credits"],
                now,
            ),
        )
        _event(
            connection,
            "generation_credit_consumed",
            now,
            user_id=reservation["user_id"],
            attempt_id=reservation["attempt_id"],
            value_integer=account["available_generation_credits"],
        )
        return self._balance_row(account)

    def release_generation(
        self,
        connection: sqlite3.Connection,
        reservation_id: str,
        reason: str,
    ) -> CreditBalance:
        reservation = connection.execute(
            "SELECT * FROM generation_credit_reservations WHERE id=?", (reservation_id,)
        ).fetchone()
        if reservation is None:
            raise InvalidInputError("Generation reservation is missing")
        if reservation["status"] == "released":
            row = connection.execute(
                "SELECT * FROM user_credit_accounts WHERE user_id=?",
                (reservation["user_id"],),
            ).fetchone()
            return self._balance_row(row)
        if reservation["status"] == "consumed":
            raise InvalidInputError("Consumed generation credit cannot be released")
        now = _iso(self.clock())
        connection.execute(
            """UPDATE generation_credit_reservations SET status='released',release_reason=?,
                   released_at=?,updated_at=? WHERE id=?""",
            (reason[:100], now, now, reservation_id),
        )
        connection.execute(
            """UPDATE generation_credit_lots
               SET reserved_credits=reserved_credits-1,available_credits=available_credits+1,
                   status='active',updated_at=? WHERE id=?""",
            (now, reservation["lot_id"]),
        )
        connection.execute(
            """UPDATE user_credit_accounts
               SET reserved_generation_credits=reserved_generation_credits-1,
                   available_generation_credits=available_generation_credits+1,
                   updated_at=?,version=version+1 WHERE user_id=?""",
            (now, reservation["user_id"]),
        )
        account = connection.execute(
            "SELECT * FROM user_credit_accounts WHERE user_id=?", (reservation["user_id"],)
        ).fetchone()
        connection.execute(
            """INSERT INTO credit_ledger(
                   user_id,delta,event_type,reference_type,reference_id,idempotency_key,
                   balance_after,reserved_after,created_at
               ) VALUES(?,1,'generation_released','attempt',?,?,?, ?,?)""",
            (
                reservation["user_id"],
                reservation["attempt_id"],
                f"credit-release:{reservation_id}",
                account["available_generation_credits"],
                account["reserved_generation_credits"],
                now,
            ),
        )
        _event(
            connection,
            "generation_credit_released",
            now,
            user_id=reservation["user_id"],
            attempt_id=reservation["attempt_id"],
            error_type=reason[:100],
            value_integer=account["available_generation_credits"],
        )
        return self._balance_row(account)

    def grant_continuation_pack(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        payment_order_id: str,
        payment_intent_id: Optional[str],
        generation_credit_quantity: int = GENERATION_CREDITS_PER_PACK,
        unlock_entitlement_quantity: int = UNLOCK_ENTITLEMENTS_PER_PACK,
    ) -> tuple[str, str]:
        if generation_credit_quantity <= 0 or unlock_entitlement_quantity <= 0:
            raise InvalidInputError("Package quantities must be positive")
        existing = connection.execute(
            "SELECT credit_lot_id,entitlement_id FROM continuation_pack_grants WHERE payment_order_id=?",
            (payment_order_id,),
        ).fetchone()
        if existing:
            return existing["credit_lot_id"], existing["entitlement_id"]
        account = self.ensure_initial_grant(connection, user_id)
        if account.available + account.reserved + generation_credit_quantity > MAX_ACCOUNT_BALANCE:
            raise InvalidInputError("Generation balance safety limit exceeded")
        now = _iso(self.clock())
        lot_id = uuid4().hex
        entitlement_ids = [uuid4().hex for _ in range(unlock_entitlement_quantity)]
        entitlement_id = entitlement_ids[0]
        grant_id = uuid4().hex
        connection.execute(
            """INSERT INTO generation_credit_lots(
                   id,user_id,source_type,source_payment_order_id,granted_credits,
                   available_credits,created_at,updated_at
               ) VALUES(?,?,'continuation_pack',?,?,?, ?,?)""",
            (
                lot_id,
                user_id,
                payment_order_id,
                generation_credit_quantity,
                generation_credit_quantity,
                now,
                now,
            ),
        )
        connection.execute(
            """UPDATE user_credit_accounts
               SET available_generation_credits=available_generation_credits+?,
                   total_generation_credits_granted=total_generation_credits_granted+?,
                   updated_at=?,version=version+1 WHERE user_id=?""",
            (generation_credit_quantity, generation_credit_quantity, now, user_id),
        )
        connection.executemany(
            """INSERT INTO unlock_entitlements(
                   id,user_id,source_payment_intent_id,source_payment_order_id,status,
                   created_at,updated_at
               ) VALUES(?,?,?,?,'available',?,?)""",
            [
                (value, user_id, payment_intent_id, payment_order_id, now, now)
                for value in entitlement_ids
            ],
        )
        connection.execute(
            """INSERT INTO continuation_pack_grants(
                   id,user_id,payment_order_id,payment_intent_id,credit_lot_id,entitlement_id,
                   generation_credit_quantity,unlock_entitlement_quantity,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                grant_id,
                user_id,
                payment_order_id,
                payment_intent_id,
                lot_id,
                entitlement_id,
                generation_credit_quantity,
                unlock_entitlement_quantity,
                now,
                now,
            ),
        )
        new_balance = account.available + generation_credit_quantity
        connection.execute(
            """INSERT INTO credit_ledger(
                   user_id,delta,event_type,reference_type,reference_id,idempotency_key,
                   balance_after,reserved_after,created_at
               ) VALUES(?,?,'continuation_pack_granted','payment_order',?,?,?, ?,?)""",
            (
                user_id,
                generation_credit_quantity,
                payment_order_id,
                f"continuation-pack:{payment_order_id}",
                new_balance,
                account.reserved,
                now,
            ),
        )
        _event(connection, "continuation_pack_paid", now, user_id=user_id, value_integer=new_balance)
        _event(
            connection,
            "unlock_entitlement_granted",
            now,
            user_id=user_id,
            value_integer=unlock_entitlement_quantity,
        )
        previous = int(
            connection.execute(
                """SELECT COUNT(*) FROM continuation_pack_grants
                   WHERE user_id=? AND payment_order_id<>?""",
                (user_id, payment_order_id),
            ).fetchone()[0]
        )
        if previous:
            _event(connection, "repeat_pack_purchase", now, user_id=user_id, value_integer=previous + 1)
        return lot_id, entitlement_id

    def unlock_version(self, user_id: str, version_id: str) -> UnlockResult:
        with self.database.read() as connection:
            version = connection.execute(
                """SELECT v.*,i.user_id,i.deleted FROM gallery_versions v
                   JOIN gallery_items i ON i.id=v.gallery_item_id WHERE v.id=?""",
                (version_id,),
            ).fetchone()
        if version is None or version["user_id"] != user_id or version["deleted"]:
            raise InvalidInputError("Gallery version does not belong to the user")
        if version["status"] != "succeeded" or not version["original_path"]:
            raise InvalidInputError("Original is not available for this version")
        original_path = Path(version["original_path"])
        if not original_path.is_file():
            raise InvalidInputError("Original file is missing; entitlement was not consumed")
        if version["unlock_status"] == "unlocked":
            return UnlockResult(version_id, original_path, version["unlock_entitlement_id"], False)
        with self.database.transaction() as connection:
            current = connection.execute(
                """SELECT v.*,i.user_id,i.deleted FROM gallery_versions v
                   JOIN gallery_items i ON i.id=v.gallery_item_id WHERE v.id=?""",
                (version_id,),
            ).fetchone()
            if current is None or current["user_id"] != user_id or current["deleted"]:
                raise InvalidInputError("Gallery version does not belong to the user")
            if current["unlock_status"] == "unlocked":
                return UnlockResult(version_id, original_path, current["unlock_entitlement_id"], False)
            entitlement = connection.execute(
                """SELECT * FROM unlock_entitlements
                   WHERE user_id=? AND status='available' ORDER BY created_at,id LIMIT 1""",
                (user_id,),
            ).fetchone()
            if entitlement is None:
                raise PaymentRequiredError("No original entitlement is available")
            now_dt = self.clock()
            now = _iso(now_dt)
            connection.execute(
                """UPDATE unlock_entitlements SET status='consumed',gallery_version_id=?,
                   consumed_at=?,updated_at=? WHERE id=? AND status='available'""",
                (version_id, now, now, entitlement["id"]),
            )
            connection.execute(
                """UPDATE gallery_versions SET unlock_status='unlocked',unlocked_at=?,
                   unlock_entitlement_id=? WHERE id=?""",
                (now, entitlement["id"], version_id),
            )
            connection.execute(
                "UPDATE generation_attempts SET result_unlocked=1 WHERE id=?",
                (current["attempt_id"],),
            )
            connection.execute(
                """UPDATE gallery_items SET unlock_status='unlocked',retention_until=?,updated_at=?
                   WHERE id=?""",
                (
                    _iso(now_dt + timedelta(days=self.paid_retention_days)),
                    now,
                    current["gallery_item_id"],
                ),
            )
            connection.execute(
                """UPDATE demo_sessions SET converted_to_paid=1,updated_at=?
                   WHERE id=(SELECT session_id FROM generation_attempts WHERE id=?)""",
                (now, current["attempt_id"]),
            )
            age_seconds = max(
                0,
                int((now_dt - datetime.fromisoformat(current["created_at"])).total_seconds()),
            )
            _event(
                connection,
                "unlock_entitlement_consumed",
                now,
                user_id=user_id,
                gallery_item_id=current["gallery_item_id"],
                value_integer=age_seconds,
            )
        return UnlockResult(version_id, original_path, entitlement["id"], True)

    def reserve_unlock_delivery(
        self, user_id: str, version_id: str
    ) -> UnlockReservation:
        """Reserve one entitlement while MAX delivery is in flight.

        An already-unlocked version needs no reservation.  A new entitlement is
        consumed only by :meth:`commit_unlock_delivery` after MAX confirms that
        the original was delivered.
        """

        with self.database.transaction() as connection:
            version = connection.execute(
                """SELECT v.*,i.user_id,i.deleted FROM gallery_versions v
                   JOIN gallery_items i ON i.id=v.gallery_item_id WHERE v.id=?""",
                (version_id,),
            ).fetchone()
            if version is None or version["user_id"] != user_id or version["deleted"]:
                raise InvalidInputError("Gallery version does not belong to the user")
            if version["status"] != "succeeded" or not version["original_path"]:
                raise InvalidInputError("Original is not available for this version")
            original_path = Path(version["original_path"])
            if not original_path.is_file():
                raise InvalidInputError("Original file is missing; entitlement was not consumed")
            if version["unlock_status"] == "unlocked":
                return UnlockReservation(
                    version_id,
                    original_path,
                    version["unlock_entitlement_id"],
                    True,
                )
            in_flight = connection.execute(
                """SELECT id FROM unlock_entitlements
                   WHERE user_id=? AND gallery_version_id=? AND status='reserved'""",
                (user_id, version_id),
            ).fetchone()
            if in_flight is not None:
                raise InvalidInputError("Original delivery is already in progress")
            entitlement = connection.execute(
                """SELECT * FROM unlock_entitlements
                   WHERE user_id=? AND status='available' ORDER BY created_at,id LIMIT 1""",
                (user_id,),
            ).fetchone()
            if entitlement is None:
                raise PaymentRequiredError("No original entitlement is available")
            now = _iso(self.clock())
            updated = connection.execute(
                """UPDATE unlock_entitlements SET status='reserved',gallery_version_id=?,
                   reserved_at=?,updated_at=? WHERE id=? AND status='available'""",
                (version_id, now, now, entitlement["id"]),
            ).rowcount
            if updated != 1:
                raise InvalidInputError("Original entitlement could not be reserved")
            return UnlockReservation(
                version_id, original_path, entitlement["id"], False
            )

    def commit_unlock_delivery(
        self, user_id: str, version_id: str, entitlement_id: str
    ) -> UnlockResult:
        """Atomically bind and consume a reserved entitlement after delivery."""

        with self.database.transaction() as connection:
            current = connection.execute(
                """SELECT v.*,i.user_id,i.deleted FROM gallery_versions v
                   JOIN gallery_items i ON i.id=v.gallery_item_id WHERE v.id=?""",
                (version_id,),
            ).fetchone()
            if current is None or current["user_id"] != user_id or current["deleted"]:
                raise InvalidInputError("Gallery version does not belong to the user")
            if current["unlock_status"] == "unlocked":
                return UnlockResult(
                    version_id,
                    Path(current["original_path"]),
                    current["unlock_entitlement_id"],
                    False,
                )
            entitlement = connection.execute(
                """SELECT * FROM unlock_entitlements
                   WHERE id=? AND user_id=? AND gallery_version_id=?""",
                (entitlement_id, user_id, version_id),
            ).fetchone()
            if entitlement is None or entitlement["status"] != "reserved":
                raise InvalidInputError("Original entitlement reservation is missing")
            original_path = Path(current["original_path"] or "")
            if not original_path.is_file():
                raise InvalidInputError("Original file is missing; entitlement was not consumed")
            now_dt = self.clock()
            now = _iso(now_dt)
            connection.execute(
                """UPDATE unlock_entitlements SET status='consumed',reserved_at=NULL,
                   consumed_at=?,updated_at=? WHERE id=? AND status='reserved'""",
                (now, now, entitlement_id),
            )
            connection.execute(
                """UPDATE gallery_versions SET unlock_status='unlocked',unlocked_at=?,
                   unlock_entitlement_id=? WHERE id=?""",
                (now, entitlement_id, version_id),
            )
            connection.execute(
                "UPDATE generation_attempts SET result_unlocked=1 WHERE id=?",
                (current["attempt_id"],),
            )
            connection.execute(
                """UPDATE gallery_items SET unlock_status='unlocked',retention_until=?,updated_at=?
                   WHERE id=?""",
                (
                    _iso(now_dt + timedelta(days=self.paid_retention_days)),
                    now,
                    current["gallery_item_id"],
                ),
            )
            connection.execute(
                """UPDATE demo_sessions SET converted_to_paid=1,updated_at=?
                   WHERE id=(SELECT session_id FROM generation_attempts WHERE id=?)""",
                (now, current["attempt_id"]),
            )
            age_seconds = max(
                0,
                int((now_dt - datetime.fromisoformat(current["created_at"])).total_seconds()),
            )
            _event(
                connection,
                "unlock_entitlement_consumed",
                now,
                user_id=user_id,
                gallery_item_id=current["gallery_item_id"],
                value_integer=age_seconds,
            )
        return UnlockResult(version_id, original_path, entitlement_id, True)

    def release_unlock_delivery(
        self, user_id: str, version_id: str, entitlement_id: str
    ) -> bool:
        """Return an in-flight entitlement after MAX rejects the original."""

        with self.database.transaction() as connection:
            entitlement = connection.execute(
                """SELECT status FROM unlock_entitlements
                   WHERE id=? AND user_id=? AND gallery_version_id=?""",
                (entitlement_id, user_id, version_id),
            ).fetchone()
            if entitlement is None or entitlement["status"] != "reserved":
                return False
            now = _iso(self.clock())
            connection.execute(
                """UPDATE unlock_entitlements SET status='available',gallery_version_id=NULL,
                   reserved_at=NULL,updated_at=? WHERE id=? AND status='reserved'""",
                (now, entitlement_id),
            )
            return True

    def refund_eligibility(self, payment_order_id: str) -> RefundEligibility:
        with self.database.read() as connection:
            grant = connection.execute(
                """SELECT g.*,l.available_credits,l.reserved_credits,l.consumed_credits,
                          COUNT(e.id) AS entitlement_count,
                          SUM(CASE WHEN e.status='available' THEN 1 ELSE 0 END)
                              AS available_entitlements
                   FROM continuation_pack_grants g
                   JOIN generation_credit_lots l ON l.id=g.credit_lot_id
                   LEFT JOIN unlock_entitlements e
                     ON e.source_payment_order_id=g.payment_order_id
                   WHERE g.payment_order_id=? GROUP BY g.id""",
                (payment_order_id,),
            ).fetchone()
        if grant is None:
            return RefundEligibility(False, "package_grant_missing")
        if grant["status"] == "refunded":
            return RefundEligibility(True, "already_refunded")
        if grant["consumed_credits"] or grant["reserved_credits"]:
            return RefundEligibility(False, "generation_credits_used_or_reserved_manual_review")
        if grant["available_entitlements"] != grant["unlock_entitlement_quantity"]:
            return RefundEligibility(False, "unlock_entitlement_used_manual_review")
        if (
            grant["entitlement_count"] != grant["unlock_entitlement_quantity"]
            or grant["available_credits"] != grant["generation_credit_quantity"]
        ):
            return RefundEligibility(False, "package_ledger_inconsistent")
        return RefundEligibility(True, "unused_package")

    def rollback_unused_pack(
        self, connection: sqlite3.Connection, payment_order_id: str
    ) -> bool:
        grant = connection.execute(
            """SELECT g.*,l.available_credits,l.reserved_credits,l.consumed_credits,
                      COUNT(e.id) AS entitlement_count,
                      SUM(CASE WHEN e.status IN ('available','reserved') THEN 1 ELSE 0 END)
                          AS refundable_entitlements
               FROM continuation_pack_grants g
               JOIN generation_credit_lots l ON l.id=g.credit_lot_id
               LEFT JOIN unlock_entitlements e
                 ON e.source_payment_order_id=g.payment_order_id
               WHERE g.payment_order_id=? GROUP BY g.id""",
            (payment_order_id,),
        ).fetchone()
        if grant is None:
            raise InvalidInputError("Package grant is missing")
        if grant["status"] == "refunded":
            return False
        if (
            grant["consumed_credits"]
            or grant["reserved_credits"]
            or grant["available_credits"] != grant["generation_credit_quantity"]
            or grant["entitlement_count"] != grant["unlock_entitlement_quantity"]
            or grant["refundable_entitlements"] != grant["unlock_entitlement_quantity"]
        ):
            raise PaymentRequiredError("Used package requires manual refund review")
        account = connection.execute(
            "SELECT * FROM user_credit_accounts WHERE user_id=?", (grant["user_id"],)
        ).fetchone()
        credit_quantity = int(grant["generation_credit_quantity"])
        if account["available_generation_credits"] < credit_quantity:
            raise InvalidInputError("Package rollback would make balance negative")
        now = _iso(self.clock())
        connection.execute(
            """UPDATE generation_credit_lots SET available_credits=0,refunded_credits=?,
               status='refunded',updated_at=? WHERE id=?""",
            (credit_quantity, now, grant["credit_lot_id"]),
        )
        connection.execute(
            """UPDATE user_credit_accounts SET
               available_generation_credits=available_generation_credits-?,
               total_generation_credits_refunded=total_generation_credits_refunded+?,
               updated_at=?,version=version+1 WHERE user_id=?""",
            (credit_quantity, credit_quantity, now, grant["user_id"]),
        )
        connection.execute(
            """UPDATE unlock_entitlements SET status='refunded',reserved_at=NULL,updated_at=?
               WHERE source_payment_order_id=? AND status IN ('available','reserved')""",
            (now, payment_order_id),
        )
        connection.execute(
            """UPDATE continuation_pack_grants SET status='refunded',updated_at=? WHERE id=?""",
            (now, grant["id"]),
        )
        connection.execute(
            """INSERT INTO credit_ledger(
                   user_id,delta,event_type,reference_type,reference_id,idempotency_key,
                   balance_after,reserved_after,created_at
               ) VALUES(?,?,'continuation_pack_refunded','payment_order',?,?,?, ?,?)""",
            (
                grant["user_id"],
                -credit_quantity,
                payment_order_id,
                f"continuation-pack-refund:{payment_order_id}",
                account["available_generation_credits"] - credit_quantity,
                account["reserved_generation_credits"],
                now,
            ),
        )
        _event(
            connection,
            "unlock_entitlement_unused",
            now,
            user_id=grant["user_id"],
            value_integer=int(grant["unlock_entitlement_quantity"]),
        )
        return True

    def hold_pack_for_refund(
        self, connection: sqlite3.Connection, payment_order_id: str
    ) -> None:
        grant = connection.execute(
            """SELECT g.*,l.available_credits,l.reserved_credits,l.consumed_credits,
                      COUNT(e.id) AS entitlement_count,
                      SUM(CASE WHEN e.status='available' THEN 1 ELSE 0 END)
                          AS available_entitlements,
                      SUM(CASE WHEN e.status='reserved' THEN 1 ELSE 0 END)
                          AS reserved_entitlements
               FROM continuation_pack_grants g
               JOIN generation_credit_lots l ON l.id=g.credit_lot_id
               LEFT JOIN unlock_entitlements e
                 ON e.source_payment_order_id=g.payment_order_id
               WHERE g.payment_order_id=? GROUP BY g.id""",
            (payment_order_id,),
        ).fetchone()
        if grant is None:
            raise InvalidInputError("Package grant is missing")
        if grant["reserved_entitlements"] == grant["unlock_entitlement_quantity"]:
            return
        if (
            grant["status"] != "active"
            or grant["available_credits"] != grant["generation_credit_quantity"]
            or grant["reserved_credits"]
            or grant["consumed_credits"]
            or grant["entitlement_count"] != grant["unlock_entitlement_quantity"]
            or grant["available_entitlements"] != grant["unlock_entitlement_quantity"]
        ):
            raise PaymentRequiredError("Used package requires manual refund review")
        now = _iso(self.clock())
        connection.execute(
            "UPDATE generation_credit_lots SET status='exhausted',updated_at=? WHERE id=?",
            (now, grant["credit_lot_id"]),
        )
        connection.execute(
            """UPDATE unlock_entitlements SET status='reserved',reserved_at=?,updated_at=?
               WHERE source_payment_order_id=? AND status='available'""",
            (now, now, payment_order_id),
        )

    def release_refund_hold(
        self, connection: sqlite3.Connection, payment_order_id: str
    ) -> None:
        grant = connection.execute(
            "SELECT * FROM continuation_pack_grants WHERE payment_order_id=?",
            (payment_order_id,),
        ).fetchone()
        if grant is None or grant["status"] != "active":
            return
        now = _iso(self.clock())
        connection.execute(
            """UPDATE generation_credit_lots SET status=CASE WHEN available_credits>0
                   THEN 'active' ELSE 'exhausted' END,updated_at=? WHERE id=?""",
            (now, grant["credit_lot_id"]),
        )
        connection.execute(
            """UPDATE unlock_entitlements SET status='available',reserved_at=NULL,updated_at=?
               WHERE source_payment_order_id=? AND status='reserved'
                 AND gallery_version_id IS NULL""",
            (now, payment_order_id),
        )

    @staticmethod
    def recover_stale_reservations(
        connection: sqlite3.Connection,
        now: str,
    ) -> int:
        rows = connection.execute(
            """SELECT r.* FROM generation_credit_reservations r
               LEFT JOIN generation_attempts a ON a.id=r.attempt_id
               WHERE r.status='reserved'
                 AND (a.id IS NULL OR a.status NOT IN ('pending','processing'))"""
        ).fetchall()
        released = 0
        for row in rows:
            connection.execute(
                """UPDATE generation_credit_reservations SET status='released',
                   release_reason='process_restarted',released_at=?,updated_at=? WHERE id=?""",
                (now, now, row["id"]),
            )
            connection.execute(
                """UPDATE generation_credit_lots SET reserved_credits=reserved_credits-1,
                   available_credits=available_credits+1,status='active',updated_at=? WHERE id=?""",
                (now, row["lot_id"]),
            )
            connection.execute(
                """UPDATE user_credit_accounts SET
                   reserved_generation_credits=reserved_generation_credits-1,
                   available_generation_credits=available_generation_credits+1,
                   updated_at=?,version=version+1 WHERE user_id=?""",
                (now, row["user_id"]),
            )
            account = connection.execute(
                "SELECT * FROM user_credit_accounts WHERE user_id=?", (row["user_id"],)
            ).fetchone()
            connection.execute(
                """INSERT OR IGNORE INTO credit_ledger(
                       user_id,delta,event_type,reference_type,reference_id,idempotency_key,
                       balance_after,reserved_after,created_at
                   ) VALUES(?,1,'generation_released','attempt',?,?,?, ?,?)""",
                (
                    row["user_id"],
                    row["attempt_id"],
                    f"credit-recover:{row['id']}",
                    account["available_generation_credits"],
                    account["reserved_generation_credits"],
                    now,
                ),
            )
            _event(
                connection,
                "generation_credit_released",
                now,
                user_id=row["user_id"],
                attempt_id=row["attempt_id"],
                error_type="process_restarted",
                value_integer=account["available_generation_credits"],
            )
            released += 1
        return released

    @staticmethod
    def recover_stale_unlock_deliveries(
        connection: sqlite3.Connection,
        now: str,
    ) -> int:
        """Release delivery reservations left by a stopped runtime.

        Refund holds also use ``reserved`` but deliberately have no selected
        gallery version, so they are not touched here.
        """

        rows = connection.execute(
            """SELECT id FROM unlock_entitlements
               WHERE status='reserved' AND gallery_version_id IS NOT NULL"""
        ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE unlock_entitlements SET status='available',
                   gallery_version_id=NULL,reserved_at=NULL,updated_at=?
                   WHERE id=? AND status='reserved'""",
                (now, row["id"]),
            )
        return len(rows)
