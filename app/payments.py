"""Version-scoped commercial payments, audit history and refund orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Protocol
from uuid import uuid4

import httpx

from app.config import Settings
from app.commerce import (
    CommerceService,
    GENERATION_CREDITS_PER_PACK,
    PRICE_MINOR,
    PRODUCT_CODE,
    UNLOCK_ENTITLEMENTS_PER_PACK,
    USER_PRODUCT_NAME,
)
from app.database import Database
from app.domain import InvalidInputError, PaymentRequiredError
from app.robokassa import (
    RobokassaError,
    RobokassaPaymentRequest,
    RobokassaProvider,
    RobokassaRefundRequest,
)


class PaymentError(RuntimeError):
    """Safe payment error suitable for operational handling."""


class PaymentUnavailable(PaymentError):
    pass


class PaymentProvider(Protocol):
    name: str
    merchant_login: str

    def payment_link(self, request: RobokassaPaymentRequest) -> str: ...
    def parse_notification(self, values: Mapping[str, str]): ...
    def create_refund(self, request: RobokassaRefundRequest): ...
    def refund_status(self, request_id: str) -> tuple[str, int]: ...


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REFUND_PENDING = "refund_pending"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class RefundStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RefundReason(StrEnum):
    CUSTOMER_REQUEST = "customer_request"
    DUPLICATE_PAYMENT = "duplicate_payment"
    TECHNICAL_FAILURE = "technical_failure"
    DELIVERY_FAILURE = "delivery_failure"
    QUALITY_DISPUTE = "quality_dispute"
    OTHER = "other"


@dataclass(frozen=True)
class PaymentIntent:
    id: str
    version_id: str
    attempt_id: str
    user_id: str
    amount_minor: int
    currency: str
    status: PaymentStatus


@dataclass(frozen=True)
class PaymentOrder:
    id: str
    public_token: str
    intent_id: str
    version_id: str
    attempt_id: str
    user_id: str
    provider: str
    provider_invoice_id: int
    amount_minor: int
    currency: str
    status: PaymentStatus
    expires_at: datetime
    payment_url: str | None = None
    product_code: str = PRODUCT_CODE
    generation_credit_quantity: int = GENERATION_CREDITS_PER_PACK
    unlock_entitlement_quantity: int = UNLOCK_ENTITLEMENTS_PER_PACK


@dataclass(frozen=True)
class PaymentAttempt:
    id: str
    order_id: str
    purpose: str
    status: str
    http_status: int | None


@dataclass(frozen=True)
class PaymentEvent:
    id: int
    order_id: str | None
    event_type: str
    status: str
    reason: str | None


@dataclass(frozen=True)
class PaymentWebhook:
    accepted: bool
    duplicate: bool
    http_status: int
    response_text: str
    order_id: str | None
    reason: str | None


@dataclass(frozen=True)
class PaymentReceipt:
    id: str
    order_id: str
    item_name: str
    amount_minor: int
    tax: str
    status: str


@dataclass(frozen=True)
class PaymentAudit:
    event_type: str
    from_status: str | None
    to_status: str | None
    reason: str | None
    created_at: str


@dataclass(frozen=True)
class PaymentHistory:
    order: PaymentOrder
    audit: tuple[PaymentAudit, ...]


@dataclass(frozen=True)
class RefundIntent:
    id: str
    order_id: str
    amount_minor: int
    currency: str
    reason: str
    status: RefundStatus
    provider_request_id: str | None


@dataclass(frozen=True)
class RefundAudit:
    event_type: str
    from_status: str | None
    to_status: str | None
    reason: str | None
    created_at: str


@dataclass(frozen=True)
class RefundHistory:
    refund: RefundIntent
    audit: tuple[RefundAudit, ...]


@dataclass(frozen=True)
class RefundPreview:
    order_id: str
    amount_minor: int
    currency: str
    reason: str
    available_minor: int
    idempotent_existing: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _safe_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _row_order(row: Mapping[str, object], payment_url: str | None = None) -> PaymentOrder:
    return PaymentOrder(
        id=str(row["id"]),
        public_token=str(row["public_token"]),
        intent_id=str(row["intent_id"]),
        version_id=str(row["version_id"]),
        attempt_id=str(row["attempt_id"]),
        user_id=str(row["user_id"]),
        provider=str(row["provider"]),
        provider_invoice_id=int(row["provider_invoice_id"]),
        amount_minor=int(row["amount_minor"]),
        currency=str(row["currency"]),
        status=PaymentStatus(str(row["status"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        payment_url=payment_url,
        product_code=str(row["product_code"]),
        generation_credit_quantity=int(row["generation_credit_quantity"]),
        unlock_entitlement_quantity=int(row["unlock_entitlement_quantity"]),
    )


class PaymentService:
    """Owns state transitions; provider callbacks never unlock broader gallery state."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        provider: PaymentProvider | None,
        *,
        clock=_now,
    ) -> None:
        self.settings = settings
        self.database = database
        self.provider = provider
        self.clock = clock
        self.commerce = CommerceService(
            database, clock, paid_retention_days=settings.paid_retention_days
        )

    def _audit(
        self,
        connection,
        order_id: str | None,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        reason: str | None,
        actor_type: str,
        actor_ref: str | None = None,
    ) -> None:
        actor_hash = (
            hashlib.sha256(actor_ref.encode("utf-8")).hexdigest() if actor_ref else None
        )
        connection.execute(
            """INSERT INTO payment_audit(
                   order_id,event_type,from_status,to_status,reason,actor_type,
                   actor_ref_hash,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                order_id, event_type, from_status, to_status, reason, actor_type,
                actor_hash, _iso(self.clock()),
            ),
        )

    def _record_product_event(
        self, connection, event_type: str, *, attempt_id=None, item_id=None,
        user_id=None, value_integer=None,
    ) -> None:
        subject_hash = (
            hashlib.sha256(f"pixora-product-subject:{user_id}".encode()).hexdigest()
            if user_id else None
        )
        connection.execute(
            """INSERT INTO product_events(
                   event_type,created_at,attempt_id,gallery_item_id,subject_hash,value_integer
               ) VALUES(?,?,?,?,?,?)""",
            (
                event_type, _iso(self.clock()), attempt_id, item_id,
                subject_hash, value_integer,
            ),
        )

    def _require_provider(self) -> PaymentProvider:
        if not self.settings.payments_enabled or self.provider is None:
            raise PaymentUnavailable("Payments are disabled")
        return self.provider

    def order_by_invoice(self, provider_invoice_id: int) -> PaymentOrder:
        """Resolve an operator-supplied invoice without exposing signed URLs."""

        if provider_invoice_id <= 0:
            raise PaymentError("Invoice must be a positive integer")
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM payment_orders WHERE provider_invoice_id=?",
                (provider_invoice_id,),
            ).fetchone()
        if row is None:
            raise PaymentError("Payment order was not found")
        return _row_order(row)

    def create_order(
        self, user_id: str, version_id: str, idempotency_key: str
    ) -> PaymentOrder:
        """Create a package order; the selected version is context, not an unlock target."""

        provider = self._require_provider()
        if not idempotency_key:
            raise PaymentError("Payment idempotency key is required")
        if self.settings.continuation_pack_price_rub * 100 != PRICE_MINOR:
            raise PaymentError("Continuation pack price must be exactly 49 RUB")
        now = self.clock()
        expires_at = now + timedelta(minutes=self.settings.payment_order_ttl_minutes)
        with self.database.transaction() as connection:
            version = connection.execute(
                """SELECT v.id,v.attempt_id,v.unlock_status,v.original_path,
                          v.gallery_item_id,a.user_id,a.status AS attempt_status,
                          i.user_id AS item_user_id
                   FROM gallery_versions v
                   JOIN generation_attempts a ON a.id=v.attempt_id
                   JOIN gallery_items i ON i.id=v.gallery_item_id
                   WHERE v.id=?""",
                (version_id,),
            ).fetchone()
            if version is None or version["user_id"] != user_id or version["item_user_id"] != user_id:
                raise PaymentError("Gallery version was not found")
            if version["attempt_status"] != "succeeded":
                raise PaymentError("Only a completed version can start a package purchase")
            replay = connection.execute(
                """SELECT o.* FROM payment_intents i
                   JOIN payment_orders o ON o.intent_id=i.id
                   WHERE i.idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["user_id"] != user_id or replay["product_code"] != PRODUCT_CODE:
                    raise PaymentError("Payment idempotency key conflict")
                order = _row_order(replay)
            else:
                connection.execute(
                    """UPDATE payment_orders SET status=?,updated_at=?,failure_code='expired'
                       WHERE user_id=? AND product_code=? AND status=? AND expires_at<?""",
                    (
                        PaymentStatus.EXPIRED.value, _iso(now), user_id, PRODUCT_CODE,
                        PaymentStatus.PENDING.value, _iso(now),
                    ),
                )
                existing = connection.execute(
                    """SELECT * FROM payment_orders
                       WHERE user_id=? AND product_code=? AND status='pending' AND expires_at>=?
                       ORDER BY created_at DESC LIMIT 1""",
                    (user_id, PRODUCT_CODE, _iso(now)),
                ).fetchone()
                if existing is not None:
                    order = _row_order(existing)
                else:
                    attempt_id = str(version["attempt_id"])
                    intent_id = uuid4().hex
                    connection.execute(
                        """INSERT INTO payment_intents(
                               id,attempt_id,idempotency_key,amount_rub,status,created_at,
                               version_id,user_id,provider,currency,updated_at,expires_at,product_code
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            intent_id, attempt_id, idempotency_key,
                            self.settings.continuation_pack_price_rub,
                            PaymentStatus.PENDING.value, _iso(now), version_id, user_id,
                            provider.name, self.settings.payment_currency, _iso(now),
                            _iso(expires_at), PRODUCT_CODE,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO payment_invoice_sequence(created_at) VALUES(?)", (_iso(now),)
                    )
                    invoice_id = int(
                        connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                    )
                    order_id = uuid4().hex
                    public_token = uuid4().hex
                    connection.execute(
                        """INSERT INTO payment_orders(
                               id,public_token,intent_id,attempt_id,version_id,user_id,provider,
                               merchant_hash,provider_invoice_id,amount_minor,currency,status,description,
                               created_at,updated_at,expires_at,product_code,
                               generation_credit_quantity,unlock_entitlement_quantity
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            order_id, public_token, intent_id, attempt_id, version_id, user_id,
                            provider.name,
                            hashlib.sha256(provider.merchant_login.encode("utf-8")).hexdigest(),
                            invoice_id, PRICE_MINOR, self.settings.payment_currency,
                            PaymentStatus.PENDING.value,
                            self.settings.payment_receipt_item_name, _iso(now), _iso(now),
                            _iso(expires_at), PRODUCT_CODE, GENERATION_CREDITS_PER_PACK,
                            UNLOCK_ENTITLEMENTS_PER_PACK,
                        ),
                    )
                    receipt_id = uuid4().hex
                    connection.execute(
                        """INSERT INTO payment_receipts(
                               id,order_id,receipt_type,item_name,quantity,amount_minor,tax,
                               payment_method,payment_object,status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            receipt_id, order_id, "payment",
                            self.settings.payment_receipt_item_name, "1", PRICE_MINOR,
                            self.settings.payment_receipt_tax,
                            "",
                            "",
                            "prepared", _iso(now), _iso(now),
                        ),
                    )
                    self._audit(
                        connection, order_id, "continuation_pack_order_created", None,
                        PaymentStatus.PENDING.value, None, "application",
                    )
                    self._record_product_event(
                        connection, "continuation_pack_payment_started",
                        attempt_id=attempt_id, item_id=version["gallery_item_id"],
                        user_id=user_id,
                    )
                    previous_paid = int(
                        connection.execute(
                            """SELECT COUNT(*) FROM payment_orders
                               WHERE user_id=? AND product_code=? AND status IN
                               ('paid','delivery_pending','delivered','refund_pending','partially_refunded')""",
                            (user_id, PRODUCT_CODE),
                        ).fetchone()[0]
                    )
                    if previous_paid == 0:
                        generations = int(
                            connection.execute(
                                """SELECT COUNT(*) FROM generation_attempts
                                   WHERE user_id=? AND status='succeeded'""",
                                (user_id,),
                            ).fetchone()[0]
                        )
                        self._record_product_event(
                            connection, "generations_before_first_purchase",
                            user_id=user_id, value_integer=generations,
                        )
                    order = PaymentOrder(
                        id=order_id, public_token=public_token, intent_id=intent_id,
                        version_id=version_id, attempt_id=attempt_id, user_id=user_id,
                        provider=provider.name, provider_invoice_id=invoice_id,
                        amount_minor=PRICE_MINOR, currency=self.settings.payment_currency,
                        status=PaymentStatus.PENDING, expires_at=expires_at,
                        product_code=PRODUCT_CODE,
                        generation_credit_quantity=GENERATION_CREDITS_PER_PACK,
                        unlock_entitlement_quantity=UNLOCK_ENTITLEMENTS_PER_PACK,
                    )
        request = RobokassaPaymentRequest(
            invoice_id=order.provider_invoice_id,
            amount_minor=order.amount_minor,
            description=USER_PRODUCT_NAME,
            public_token=order.public_token,
            expires_at=order.expires_at,
            receipt_name=self.settings.payment_receipt_item_name,
            receipt_tax=self.settings.payment_receipt_tax,
        )
        payment_url = provider.payment_link(request)
        attempt_key = hashlib.sha256(
            f"link:{order.id}:{idempotency_key}".encode("utf-8")
        ).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO payment_attempts(
                       id,order_id,purpose,idempotency_key,request_digest,status,
                       started_at,completed_at
                   ) VALUES(?,?,?,?,?,'succeeded',?,?)""",
                (
                    uuid4().hex, order.id, "create_link", attempt_key,
                    hashlib.sha256(payment_url.encode("utf-8")).hexdigest(),
                    _iso(now), _iso(self.clock()),
                ),
            )
        return PaymentOrder(**{**order.__dict__, "payment_url": payment_url})

    def process_webhook(
        self,
        values: Mapping[str, str],
        *,
        method: str,
        path: str,
        source: str | None = None,
    ) -> PaymentWebhook:
        provider = self._require_provider()
        now = self.clock()
        notification = provider.parse_notification(values)
        body_hash = hashlib.sha256(_safe_json(dict(sorted(values.items()))).encode("utf-8")).hexdigest()
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest() if source else None
        with self.database.transaction() as connection:
            duplicate = connection.execute(
                "SELECT * FROM payment_events WHERE event_digest=?",
                (notification.event_digest,),
            ).fetchone()
            if duplicate is not None:
                order = (
                    connection.execute(
                        "SELECT provider_invoice_id FROM payment_orders WHERE id=?",
                        (duplicate["order_id"],),
                    ).fetchone() if duplicate["order_id"] else None
                )
                response = f"OK{order['provider_invoice_id']}" if order and duplicate["status"] == "processed" else "REJECTED"
                return PaymentWebhook(
                    accepted=bool(order and duplicate["status"] == "processed"),
                    duplicate=True,
                    http_status=200 if order and duplicate["status"] == "processed" else 409,
                    response_text=response,
                    order_id=duplicate["order_id"],
                    reason="duplicate_event",
                )
            order = connection.execute(
                "SELECT * FROM payment_orders WHERE provider_invoice_id=?",
                (notification.invoice_id,),
            ).fetchone() if notification.invoice_id is not None else None
            order_id = str(order["id"]) if order else None
            event_cursor = connection.execute(
                """INSERT INTO payment_events(
                       order_id,provider,event_type,event_digest,provider_event_id,
                       received_at,status,payload_safe_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    order_id, provider.name, "result_url", notification.event_digest,
                    notification.operation_key, _iso(now), "received",
                    _safe_json({
                        "has_invoice": notification.invoice_id is not None,
                        "has_amount": notification.amount_minor is not None,
                        "has_payment_method": bool(notification.payment_method),
                    }),
                ),
            )
            event_id = int(event_cursor.lastrowid)
            invoice_valid = order is not None
            signature_valid = notification.signature_valid
            merchant_valid = bool(
                order
                and order["provider"] == provider.name
                and provider.merchant_login
                and order["merchant_hash"] == hashlib.sha256(
                    provider.merchant_login.encode("utf-8")
                ).hexdigest()
            )
            amount_valid = bool(order and notification.amount_minor == order["amount_minor"])
            currency_valid = bool(order and order["currency"] == "RUB")
            status_valid = True  # Classic ResultURL is the provider's success notification.
            # A signed ResultURL for a known invoice may arrive after the local link
            # TTL. The TTL stops link reuse; it must never turn a real payment into
            # money received without product delivery.
            timestamp_valid = order is not None
            token_valid = bool(order and notification.public_token == order["public_token"])
            replay_valid = True
            reason = next((name for name, valid in (
                ("unknown_invoice", invoice_valid),
                ("invalid_signature", signature_valid),
                ("merchant_mismatch", merchant_valid),
                ("amount_mismatch", amount_valid),
                ("currency_mismatch", currency_valid),
                ("invalid_status", status_valid),
                ("expired_invoice", timestamp_valid),
                ("order_token_mismatch", token_valid),
            ) if not valid), None)
            accepted = reason is None
            duplicate_payment = bool(order and order["status"] in {
                PaymentStatus.PAID.value,
                PaymentStatus.DELIVERY_PENDING.value,
                PaymentStatus.DELIVERED.value,
                PaymentStatus.REFUND_PENDING.value,
                PaymentStatus.PARTIALLY_REFUNDED.value,
                PaymentStatus.REFUNDED.value,
            })
            if duplicate_payment and accepted:
                reason = "duplicate_payment"
            http_status = 200 if accepted else 400
            response = f"OK{notification.invoice_id}" if accepted else "REJECTED"
            connection.execute(
                """INSERT INTO payment_webhooks(
                       id,event_id,request_method,request_path,source_hash,signature_valid,
                       merchant_valid,invoice_valid,amount_valid,currency_valid,status_valid,
                       timestamp_valid,replay_valid,body_hash,http_status,response_code,received_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid4().hex, event_id, method.upper(), path, source_hash,
                    int(signature_valid), int(merchant_valid), int(invoice_valid),
                    int(amount_valid), int(currency_valid), int(status_valid),
                    int(timestamp_valid), int(replay_valid), body_hash, http_status,
                    "ok" if accepted else str(reason), _iso(now),
                ),
            )
            if not accepted:
                connection.execute(
                    """UPDATE payment_events SET status='rejected',reason=?,processed_at=?
                       WHERE id=?""",
                    (reason, _iso(now), event_id),
                )
                self._audit(
                    connection, order_id, "webhook_rejected",
                    str(order["status"]) if order else None,
                    str(order["status"]) if order else None,
                    reason, "provider", source,
                )
                if order is not None:
                    item = connection.execute(
                        "SELECT gallery_item_id FROM gallery_versions WHERE id=?",
                        (order["version_id"],),
                    ).fetchone()
                    self._record_product_event(
                        connection, "continuation_pack_failed",
                        attempt_id=order["attempt_id"],
                        item_id=item["gallery_item_id"] if item else None,
                        user_id=order["user_id"],
                    )
                return PaymentWebhook(False, False, http_status, response, order_id, reason)
            previous = str(order["status"])
            if not duplicate_payment:
                connection.execute(
                    """UPDATE payment_orders SET status='paid',paid_at=?,updated_at=?,
                           provider_payment_id=COALESCE(?,provider_payment_id)
                       WHERE id=?""",
                    (_iso(now), _iso(now), notification.operation_key, order_id),
                )
                connection.execute(
                    """UPDATE payment_intents SET status='paid',confirmed_at=?,updated_at=?
                       WHERE id=?""",
                    (_iso(now), _iso(now), order["intent_id"]),
                )
                connection.execute(
                    "UPDATE payment_receipts SET status='payment_confirmed',updated_at=? WHERE order_id=?",
                    (_iso(now), order_id),
                )
                item = connection.execute(
                    "SELECT gallery_item_id FROM gallery_versions WHERE id=?", (order["version_id"],)
                ).fetchone()
                self.commerce.grant_continuation_pack(
                    connection,
                    user_id=order["user_id"],
                    payment_order_id=order_id,
                    payment_intent_id=order["intent_id"],
                )
                self._record_product_event(
                    connection, "packs_per_payer", attempt_id=order["attempt_id"],
                    item_id=item["gallery_item_id"] if item else None,
                    user_id=order["user_id"],
                    value_integer=int(
                        connection.execute(
                            """SELECT COUNT(*) FROM continuation_pack_grants
                               WHERE user_id=? AND status='active'""",
                            (order["user_id"],),
                        ).fetchone()[0]
                    ),
                )
            connection.execute(
                """UPDATE payment_events SET status='processed',reason=?,processed_at=?
                   WHERE id=?""",
                (reason, _iso(now), event_id),
            )
            self._audit(
                connection, order_id,
                "duplicate_payment" if duplicate_payment else "payment_confirmed",
                previous, previous if duplicate_payment else PaymentStatus.PAID.value,
                reason, "provider", source,
            )
        return PaymentWebhook(True, duplicate_payment, 200, response, order_id, reason)

    def original_for_order(self, order_id: str, user_id: str) -> Path:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT o.status,o.user_id,v.original_path,v.unlock_status
                   FROM payment_orders o JOIN gallery_versions v ON v.id=o.version_id
                   WHERE o.id=?""",
                (order_id,),
            ).fetchone()
        if row is None or row["user_id"] != user_id:
            raise PaymentError("Payment order was not found")
        if row["status"] not in {
            PaymentStatus.PAID.value, PaymentStatus.DELIVERY_PENDING.value,
            PaymentStatus.DELIVERED.value, PaymentStatus.REFUND_PENDING.value,
            PaymentStatus.PARTIALLY_REFUNDED.value,
        } or row["unlock_status"] != "unlocked":
            raise PaymentError("The exact version is not paid")
        original = Path(row["original_path"])
        if not original.is_file():
            raise PaymentError("The paid original file is unavailable")
        return original

    def mark_delivery(self, order_id: str, *, delivered: bool, error_code: str | None = None) -> None:
        now = self.clock()
        with self.database.transaction() as connection:
            order = connection.execute("SELECT * FROM payment_orders WHERE id=?", (order_id,)).fetchone()
            if order is None or order["status"] not in {
                PaymentStatus.PAID.value, PaymentStatus.DELIVERY_PENDING.value,
                PaymentStatus.DELIVERED.value,
            }:
                raise PaymentError("Order is not deliverable")
            attempt_id = uuid4().hex
            connection.execute(
                """INSERT INTO payment_attempts(
                       id,order_id,purpose,idempotency_key,status,started_at,completed_at,error_code
                   ) VALUES(?,?, 'deliver_original', ?,?,?,?,?)""",
                (
                    attempt_id, order_id, f"delivery:{attempt_id}",
                    "succeeded" if delivered else "failed", _iso(now), _iso(now), error_code,
                ),
            )
            next_status = PaymentStatus.DELIVERED.value if delivered else PaymentStatus.DELIVERY_PENDING.value
            connection.execute(
                """UPDATE payment_orders SET status=?,updated_at=?,delivered_at=CASE WHEN ? THEN ? ELSE delivered_at END,
                       failure_code=? WHERE id=?""",
                (next_status, _iso(now), int(delivered), _iso(now), error_code, order_id),
            )
            if delivered:
                connection.execute(
                    """UPDATE gallery_versions SET delivery_count=delivery_count+1,last_delivered_at=?
                       WHERE id=?""",
                    (_iso(now), order["version_id"]),
                )
            self._audit(
                connection, order_id,
                "original_delivered" if delivered else "original_delivery_failed",
                str(order["status"]), next_status, error_code, "application",
            )
            item = connection.execute(
                "SELECT gallery_item_id FROM gallery_versions WHERE id=?", (order["version_id"],)
            ).fetchone()
            self._record_product_event(
                connection, "original_delivered" if delivered else "original_delivery_failed",
                attempt_id=order["attempt_id"], item_id=item["gallery_item_id"] if item else None,
            )

    def schedule_delivery_retry(self, order_id: str) -> PaymentOrder:
        """Audit an operator decision to retry an already-paid failed delivery."""

        with self.database.transaction() as connection:
            order = connection.execute(
                "SELECT * FROM payment_orders WHERE id=?", (order_id,)
            ).fetchone()
            if order is None:
                raise PaymentError("Payment order was not found")
            if order["status"] != PaymentStatus.DELIVERY_PENDING.value:
                raise PaymentError("Only a delivery-pending order can be scheduled for retry")
            self._audit(
                connection,
                order_id,
                "delivery_retry_scheduled",
                PaymentStatus.DELIVERY_PENDING.value,
                PaymentStatus.DELIVERY_PENDING.value,
                "operator_requested",
                "operator",
            )
        return _row_order(order)

    def preview_refund(
        self,
        order_id: str,
        amount_minor: int,
        reason: str,
        idempotency_key: str,
    ) -> RefundPreview:
        """Validate a refund request without creating or changing any records."""

        if amount_minor <= 0 or not reason.strip() or not idempotency_key:
            raise PaymentError("Refund amount, reason and idempotency key are required")
        try:
            safe_reason = RefundReason(reason.strip()).value
        except ValueError as exc:
            raise PaymentError("Refund reason is not supported") from exc
        with self.database.read() as connection:
            existing = connection.execute(
                "SELECT * FROM refund_intents WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["order_id"]) != order_id
                    or int(existing["amount_minor"]) != amount_minor
                    or str(existing["reason"]) != safe_reason
                ):
                    raise PaymentError("Refund idempotency key conflict")
                order = connection.execute(
                    "SELECT * FROM payment_orders WHERE id=?", (order_id,)
                ).fetchone()
                if order is None:
                    raise PaymentError("Payment order was not found")
                available = int(order["amount_minor"]) - int(order["refunded_amount_minor"])
                return RefundPreview(
                    order_id,
                    amount_minor,
                    str(order["currency"]),
                    safe_reason,
                    available,
                    True,
                )
            order = connection.execute(
                "SELECT * FROM payment_orders WHERE id=?", (order_id,)
            ).fetchone()
        if order is None or order["status"] not in {
            PaymentStatus.PAID.value,
            PaymentStatus.DELIVERY_PENDING.value,
            PaymentStatus.DELIVERED.value,
            PaymentStatus.PARTIALLY_REFUNDED.value,
        }:
            raise PaymentError("Only a paid order can be refunded")
        available = int(order["amount_minor"]) - int(order["refunded_amount_minor"])
        if amount_minor > available:
            raise PaymentError("Refund exceeds the remaining paid amount")
        if order["product_code"] == PRODUCT_CODE:
            if amount_minor != int(order["amount_minor"]):
                raise PaymentError("Continuation pack partial refund requires manual review")
            eligibility = self.commerce.refund_eligibility(order_id)
            if not eligibility.eligible:
                raise PaymentError(eligibility.reason)
        return RefundPreview(
            order_id,
            amount_minor,
            str(order["currency"]),
            safe_reason,
            available,
            False,
        )

    def prepare_refund(
        self,
        order_id: str,
        amount_minor: int,
        reason: str,
        idempotency_key: str,
    ) -> RefundIntent:
        if amount_minor <= 0 or not reason.strip() or not idempotency_key:
            raise PaymentError("Refund amount, reason and idempotency key are required")
        try:
            safe_reason = RefundReason(reason.strip()).value
        except ValueError as exc:
            raise PaymentError("Refund reason is not supported") from exc
        self.preview_refund(order_id, amount_minor, safe_reason, idempotency_key)
        now = self.clock()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM refund_intents WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["order_id"]) != order_id
                    or int(existing["amount_minor"]) != amount_minor
                    or str(existing["reason"]) != safe_reason
                ):
                    raise PaymentError("Refund idempotency key conflict")
                return RefundIntent(
                    str(existing["id"]), str(existing["order_id"]), int(existing["amount_minor"]),
                    str(existing["currency"]), str(existing["reason"]),
                    RefundStatus(str(existing["status"])), existing["provider_request_id"],
                )
            order = connection.execute("SELECT * FROM payment_orders WHERE id=?", (order_id,)).fetchone()
            if order is None or order["status"] not in {
                PaymentStatus.PAID.value, PaymentStatus.DELIVERY_PENDING.value,
                PaymentStatus.DELIVERED.value, PaymentStatus.PARTIALLY_REFUNDED.value,
            }:
                raise PaymentError("Only a paid order can be refunded")
            available = int(order["amount_minor"]) - int(order["refunded_amount_minor"])
            if amount_minor > available:
                raise PaymentError("Refund exceeds the remaining paid amount")
            refund_id = uuid4().hex
            connection.execute(
                """INSERT INTO refund_intents(
                       id,order_id,amount_minor,currency,reason,status,idempotency_key,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,'draft',?,?,?)""",
                (
                    refund_id, order_id, amount_minor, order["currency"], safe_reason,
                    idempotency_key, _iso(now), _iso(now),
                ),
            )
            connection.execute(
                """INSERT INTO refund_audit(
                       refund_id,event_type,from_status,to_status,reason,actor_type,created_at
                   ) VALUES(?, 'refund_prepared', NULL, 'draft', ?, 'operator', ?)""",
                (refund_id, safe_reason, _iso(now)),
            )
            self._record_product_event(
                connection, "refund_started", attempt_id=order["attempt_id"]
            )
        return RefundIntent(
            refund_id, order_id, amount_minor, str(order["currency"]), safe_reason,
            RefundStatus.DRAFT, None,
        )

    def submit_refund(self, refund_id: str) -> RefundIntent:
        provider = self._require_provider()
        if not self.settings.payment_refunds_enabled:
            raise PaymentUnavailable("Refund execution is disabled")
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT r.*,o.provider_payment_id,o.attempt_id,o.product_code,
                          p.item_name,p.tax
                   FROM refund_intents r JOIN payment_orders o ON o.id=r.order_id
                   JOIN payment_receipts p ON p.order_id=o.id AND p.receipt_type='payment'
                   WHERE r.id=?""",
                (refund_id,),
            ).fetchone()
        if row is None or row["status"] != RefundStatus.DRAFT.value:
            raise PaymentError("Refund is not in draft state")
        if not row["provider_payment_id"]:
            raise PaymentError("Refund requires the Robokassa operation key")
        if row["product_code"] == PRODUCT_CODE:
            with self.database.transaction() as connection:
                try:
                    self.commerce.hold_pack_for_refund(
                        connection, str(row["order_id"])
                    )
                except (InvalidInputError, PaymentRequiredError) as exc:
                    raise PaymentError(str(exc)) from exc
        try:
            result = provider.create_refund(RobokassaRefundRequest(
                operation_key=str(row["provider_payment_id"]),
                amount_minor=int(row["amount_minor"]),
                item_name=str(row["item_name"]),
                tax=str(row["tax"]),
            ))
        except RobokassaError as exc:
            if row["product_code"] == PRODUCT_CODE:
                with self.database.transaction() as connection:
                    self.commerce.release_refund_hold(
                        connection, str(row["order_id"])
                    )
            raise PaymentError("Refund provider request failed") from exc
        now = self.clock()
        status = RefundStatus.PENDING if result.accepted else RefundStatus.FAILED
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO payment_attempts(
                       id,order_id,purpose,idempotency_key,provider_request_id,status,
                       http_status,started_at,completed_at,error_code
                   ) VALUES(?,?, 'submit_refund', ?,?,?,?,?,?,?)""",
                (
                    uuid4().hex, row["order_id"], f"refund:{refund_id}",
                    result.request_id, "succeeded" if result.accepted else "failed",
                    result.http_status, _iso(now), _iso(now), result.error_code,
                ),
            )
            connection.execute(
                """UPDATE refund_intents SET status=?,provider_request_id=?,updated_at=?
                   WHERE id=?""",
                (status.value, result.request_id, _iso(now), refund_id),
            )
            connection.execute(
                """INSERT INTO refund_audit(
                       refund_id,event_type,from_status,to_status,reason,actor_type,created_at
                   ) VALUES(?, 'refund_submitted', 'draft', ?, ?, 'application', ?)""",
                (refund_id, status.value, result.error_code, _iso(now)),
            )
            if result.accepted:
                connection.execute(
                    "UPDATE payment_orders SET status='refund_pending',updated_at=? WHERE id=?",
                    (_iso(now), row["order_id"]),
                )
            else:
                if row["product_code"] == PRODUCT_CODE:
                    self.commerce.release_refund_hold(
                        connection, str(row["order_id"])
                    )
                self._record_product_event(
                    connection, "refund_failed", attempt_id=row["attempt_id"]
                )
        return RefundIntent(
            refund_id, str(row["order_id"]), int(row["amount_minor"]),
            str(row["currency"]), str(row["reason"]), status, result.request_id,
        )

    def refresh_refund(self, refund_id: str) -> RefundIntent:
        provider = self._require_provider()
        if not self.settings.payment_refunds_enabled:
            raise PaymentUnavailable("Refund execution is disabled")
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM refund_intents WHERE id=?", (refund_id,)
            ).fetchone()
        if row is None:
            raise PaymentError("Refund was not found")
        if row["status"] in {
            RefundStatus.SUCCEEDED.value, RefundStatus.FAILED.value,
            RefundStatus.CANCELLED.value,
        }:
            return RefundIntent(
                str(row["id"]), str(row["order_id"]), int(row["amount_minor"]),
                str(row["currency"]), str(row["reason"]),
                RefundStatus(str(row["status"])), row["provider_request_id"],
            )
        if not row["provider_request_id"]:
            raise PaymentError("Refund has no provider request id")
        try:
            label, _http_status = provider.refund_status(str(row["provider_request_id"]))
        except RobokassaError as exc:
            raise PaymentError("Refund provider status failed") from exc
        mapped = {
            "finished": RefundStatus.SUCCEEDED,
            "processing": RefundStatus.PROCESSING,
            "canceled": RefundStatus.CANCELLED,
        }.get(label, RefundStatus.FAILED)
        now = self.clock()
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE refund_intents SET status=?,updated_at=?,
                       completed_at=CASE WHEN ? IN ('succeeded','failed','cancelled') THEN ? ELSE completed_at END
                   WHERE id=?""",
                (mapped.value, _iso(now), mapped.value, _iso(now), refund_id),
            )
            connection.execute(
                """INSERT INTO refund_audit(
                       refund_id,event_type,from_status,to_status,reason,actor_type,created_at
                   ) VALUES(?, 'refund_status', ?, ?, ?, 'provider', ?)""",
                (refund_id, row["status"], mapped.value, label[:100], _iso(now)),
            )
            if mapped == RefundStatus.SUCCEEDED:
                order = connection.execute(
                    "SELECT * FROM payment_orders WHERE id=?", (row["order_id"],)
                ).fetchone()
                refunded = min(
                    int(order["amount_minor"]),
                    int(order["refunded_amount_minor"]) + int(row["amount_minor"]),
                )
                order_status = (
                    PaymentStatus.REFUNDED.value
                    if refunded == int(order["amount_minor"])
                    else PaymentStatus.PARTIALLY_REFUNDED.value
                )
                connection.execute(
                    """UPDATE payment_orders SET refunded_amount_minor=?,status=?,updated_at=?
                       WHERE id=?""",
                    (refunded, order_status, _iso(now), row["order_id"]),
                )
                if (
                    order_status == PaymentStatus.REFUNDED.value
                    and order["product_code"] == PRODUCT_CODE
                ):
                    try:
                        self.commerce.rollback_unused_pack(
                            connection, str(row["order_id"])
                        )
                    except (InvalidInputError, PaymentRequiredError) as exc:
                        raise PaymentError(
                            "Refund completed but package rollback requires operator intervention"
                        ) from exc
                elif order_status == PaymentStatus.REFUNDED.value:
                    connection.execute(
                        """UPDATE gallery_versions SET unlock_status='refunded'
                           WHERE id=? AND payment_order_id=?""",
                        (order["version_id"], row["order_id"]),
                    )
                    connection.execute(
                        "UPDATE generation_attempts SET result_unlocked=0 WHERE id=?",
                        (order["attempt_id"],),
                    )
                connection.execute(
                    """INSERT INTO payment_receipts(
                           id,order_id,receipt_type,item_name,quantity,amount_minor,tax,
                           payment_method,payment_object,status,created_at,updated_at
                       ) SELECT ?,id,'refund',description,'1',?, ?,
                                'full_payment','service','refund_confirmed',?,?
                         FROM payment_orders WHERE id=?""",
                    (
                        uuid4().hex, row["amount_minor"], self.settings.payment_receipt_tax,
                        _iso(now), _iso(now), row["order_id"],
                    ),
                )
                self._audit(
                    connection, str(row["order_id"]), "refund_confirmed",
                    str(order["status"]), order_status, None, "provider",
                )
                item = connection.execute(
                    "SELECT gallery_item_id FROM gallery_versions WHERE payment_order_id=?",
                    (row["order_id"],),
                ).fetchone()
                self._record_product_event(
                    connection, "refund_confirmed", attempt_id=order["attempt_id"],
                    item_id=item["gallery_item_id"] if item else None,
                )
            elif mapped in {RefundStatus.FAILED, RefundStatus.CANCELLED}:
                order_for_hold = connection.execute(
                    "SELECT product_code FROM payment_orders WHERE id=?",
                    (row["order_id"],),
                ).fetchone()
                if order_for_hold and order_for_hold["product_code"] == PRODUCT_CODE:
                    self.commerce.release_refund_hold(
                        connection, str(row["order_id"])
                    )
                connection.execute(
                    """UPDATE payment_orders SET status=CASE
                           WHEN delivered_at IS NULL THEN 'paid' ELSE 'delivered' END,
                           updated_at=? WHERE id=?""",
                    (_iso(now), row["order_id"]),
                )
                order = connection.execute(
                    "SELECT attempt_id FROM payment_orders WHERE id=?", (row["order_id"],)
                ).fetchone()
                self._record_product_event(
                    connection, "refund_failed", attempt_id=order["attempt_id"] if order else None,
                )
        return RefundIntent(
            str(row["id"]), str(row["order_id"]), int(row["amount_minor"]),
            str(row["currency"]), str(row["reason"]), mapped,
            str(row["provider_request_id"]),
        )

    def history(self, order_id: str) -> PaymentHistory:
        with self.database.read() as connection:
            order = connection.execute("SELECT * FROM payment_orders WHERE id=?", (order_id,)).fetchone()
            rows = connection.execute(
                """SELECT event_type,from_status,to_status,reason,created_at
                   FROM payment_audit WHERE order_id=? ORDER BY id""",
                (order_id,),
            ).fetchall()
        if order is None:
            raise PaymentError("Payment order was not found")
        return PaymentHistory(
            _row_order(order),
            tuple(PaymentAudit(
                str(row["event_type"]), row["from_status"], row["to_status"],
                row["reason"], str(row["created_at"]),
            ) for row in rows),
        )

    def refund_history(self, refund_id: str) -> RefundHistory:
        with self.database.read() as connection:
            refund_row = connection.execute(
                "SELECT * FROM refund_intents WHERE id=?", (refund_id,)
            ).fetchone()
            rows = connection.execute(
                """SELECT event_type,from_status,to_status,reason,created_at
                   FROM refund_audit WHERE refund_id=? ORDER BY id""",
                (refund_id,),
            ).fetchall()
        if refund_row is None:
            raise PaymentError("Refund was not found")
        refund = RefundIntent(
            id=str(refund_row["id"]), order_id=str(refund_row["order_id"]),
            amount_minor=int(refund_row["amount_minor"]),
            currency=str(refund_row["currency"]), reason=str(refund_row["reason"]),
            status=RefundStatus(str(refund_row["status"])),
            provider_request_id=refund_row["provider_request_id"],
        )
        return RefundHistory(
            refund,
            tuple(RefundAudit(
                str(row["event_type"]), row["from_status"], row["to_status"],
                row["reason"], str(row["created_at"]),
            ) for row in rows),
        )


def build_payment_service(
    settings: Settings,
    database: Database,
    *,
    client: httpx.Client | None = None,
    clock=_now,
) -> PaymentService:
    provider = None
    if settings.payment_provider == "robokassa" or settings.payments_enabled:
        provider = RobokassaProvider(
            merchant_login=settings.robokassa_merchant_login,
            password1=settings.robokassa_password1,
            password2=settings.robokassa_password2,
            password3=settings.robokassa_password3,
            hash_algorithm=settings.robokassa_hash_algorithm,
            mode=settings.robokassa_mode,
            payment_url=settings.robokassa_payment_url,
            refund_url=settings.robokassa_refund_url,
            refund_status_url=settings.robokassa_refund_status_url,
            success_url=settings.payment_success_url,
            fail_url=settings.payment_fail_url,
            client=client,
        )
    return PaymentService(settings, database, provider, clock=clock)
