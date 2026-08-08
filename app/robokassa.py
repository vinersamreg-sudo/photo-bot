"""Robokassa payment boundary with deterministic signatures and no secret logging."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping
from urllib.parse import quote_plus, urlencode

import httpx


ROBOKASSA_TIMEZONE = timezone(timedelta(hours=3))


class RobokassaError(RuntimeError):
    """Safe integration error; provider payloads and credentials are never included."""


@dataclass(frozen=True)
class RobokassaPaymentRequest:
    invoice_id: int
    amount_minor: int
    description: str
    public_token: str
    expires_at: datetime
    receipt_name: str
    receipt_tax: str
    success_url: str = ""
    fail_url: str = ""


@dataclass(frozen=True)
class RobokassaPaymentForm:
    """Signed provider form assembled once for either POST or legacy GET use."""

    action_url: str
    fields: tuple[tuple[str, str], ...]
    signature_base_redacted: str

    def as_url(self) -> str:
        return f"{self.action_url}?{urlencode(self.fields)}"


@dataclass(frozen=True)
class RobokassaNotification:
    invoice_id: int | None
    amount_minor: int | None
    signature_valid: bool
    public_token: str
    payment_method: str | None
    operation_key: str | None
    event_digest: str


@dataclass(frozen=True)
class RobokassaRefundRequest:
    operation_key: str
    amount_minor: int
    item_name: str
    tax: str


@dataclass(frozen=True)
class RobokassaRefundResult:
    accepted: bool
    request_id: str | None
    error_code: str | None
    http_status: int


def amount_text(amount_minor: int, *, decimals: int = 2) -> str:
    if amount_minor <= 0:
        raise ValueError("Payment amount must be positive")
    return f"{Decimal(amount_minor) / Decimal(100):.{decimals}f}"


def parse_amount_minor(raw: object) -> int | None:
    try:
        value = Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return int(value * 100)


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class RobokassaProvider:
    """Classic payment form/ResultURL and the separate refund API."""

    name = "robokassa"

    def __init__(
        self,
        *,
        merchant_login: str,
        password1: str,
        password2: str,
        password3: str = "",
        hash_algorithm: str = "sha256",
        mode: str = "sandbox",
        payment_url: str = "https://auth.robokassa.ru/Merchant/Index.aspx",
        refund_url: str = "https://services.robokassa.ru/RefundService/Refund/Create",
        refund_status_url: str = "https://services.robokassa.ru/RefundService/Refund/GetState",
        success_url: str = "",
        fail_url: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        if hash_algorithm != "sha256":
            raise ValueError("Robokassa hash algorithm must be sha256")
        if mode not in {"sandbox", "production"}:
            raise ValueError("Unsupported Robokassa mode")
        self.merchant_login = merchant_login
        self.password1 = password1
        self.password2 = password2
        self.password3 = password3
        self.hash_algorithm = hash_algorithm
        self.mode = mode
        self.payment_url = payment_url
        self.refund_url = refund_url
        self.refund_status_url = refund_status_url
        self.success_url = success_url
        self.fail_url = fail_url
        self.client = client or httpx.Client(timeout=30)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _digest(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest().upper()

    @staticmethod
    def _receipt(request: RobokassaPaymentRequest) -> str:
        name = request.receipt_name.strip()
        if not name or len(name) > 128:
            raise ValueError("Receipt item name must contain 1 to 128 characters")
        if not request.receipt_tax:
            raise ValueError("Receipt tax must be configured")
        # Robokassa requires JSON numbers. Render the canonical two-decimal value
        # directly from integer minor units so no binary float enters the receipt.
        money = amount_text(request.amount_minor)
        return (
            '{"items":[{'
            f'"name":{json.dumps(name, ensure_ascii=False)},'
            '"quantity":1,'
            f'"sum":{money},'
            f'"tax":{json.dumps(request.receipt_tax)}'
            '}]}'
        )

    @staticmethod
    def _shp(params: Mapping[str, str]) -> str:
        return "".join(f":{key}={params[key]}" for key in sorted(params))

    def payment_form(self, request: RobokassaPaymentRequest) -> RobokassaPaymentForm:
        if request.expires_at.tzinfo is None:
            raise ValueError("Robokassa expiration date must be timezone-aware")
        expiration_date = request.expires_at.astimezone(
            ROBOKASSA_TIMEZONE
        ).strftime("%Y-%m-%dT%H:%M")
        # Receipt is passed as the provider's once-encoded JSON value. ReturnURL
        # modifiers, however, must be signed exactly as they appear in the POST
        # fields; the browser applies application/x-www-form-urlencoded escaping
        # to the transport after SignatureValue has already been calculated.
        receipt_encoded = quote_plus(self._receipt(request), safe="")
        shp = {"Shp_order": request.public_token}
        signature_parts = [
            self.merchant_login,
            amount_text(request.amount_minor),
            str(request.invoice_id),
            receipt_encoded,
        ]
        if bool(request.success_url) != bool(request.fail_url):
            raise ValueError("Robokassa return URLs must be configured together")
        if request.success_url:
            signature_parts.extend((
                request.success_url,
                "GET",
                request.fail_url,
                "GET",
            ))
        signature_parts.append(self.password1)
        base = ":".join(signature_parts) + self._shp(shp)
        redacted_parts = [*signature_parts[:-1], "[PASSWORD1]"]
        redacted_base = ":".join(redacted_parts) + self._shp(shp)
        params: dict[str, str] = {
            "MerchantLogin": self.merchant_login,
            "OutSum": amount_text(request.amount_minor),
            "InvId": str(request.invoice_id),
            "Description": request.description[:100],
            "SignatureValue": self._digest(base),
            "Receipt": receipt_encoded,
            "Culture": "ru",
            "Encoding": "utf-8",
            # The classic Robokassa form does not accept an offset in this
            # parameter and interprets the wall-clock value as Moscow time.
            "ExpirationDate": expiration_date,
            **shp,
        }
        if self.mode == "sandbox":
            params["IsTest"] = "1"
        if request.success_url:
            params.update({
                "SuccessUrl2": request.success_url,
                "SuccessUrl2Method": "GET",
                "FailUrl2": request.fail_url,
                "FailUrl2Method": "GET",
            })
        return RobokassaPaymentForm(
            action_url=self.payment_url,
            fields=tuple(params.items()),
            signature_base_redacted=redacted_base,
        )

    def payment_link(self, request: RobokassaPaymentRequest) -> str:
        """Build the legacy GET representation for internal compatibility."""

        return self.payment_form(request).as_url()

    def parse_notification(self, values: Mapping[str, str]) -> RobokassaNotification:
        raw_invoice = values.get("InvId") or values.get("InvID") or ""
        try:
            invoice_id = int(raw_invoice)
        except (TypeError, ValueError):
            invoice_id = None
        raw_amount = values.get("OutSum") or ""
        public_token = values.get("Shp_order", "")
        shp = {
            key: str(value)
            for key, value in values.items()
            if key.startswith("Shp_")
        }
        base = f"{raw_amount}:{raw_invoice}:{self.password2}{self._shp(shp)}"
        supplied = values.get("SignatureValue", "")
        signature_valid = bool(supplied) and hmac.compare_digest(
            supplied.upper(), self._digest(base)
        )
        canonical = _compact_json({
            "amount": raw_amount,
            "invoice": raw_invoice,
            "public_token": public_token,
            "signature": supplied.upper(),
        })
        return RobokassaNotification(
            invoice_id=invoice_id,
            amount_minor=parse_amount_minor(raw_amount),
            signature_valid=signature_valid,
            public_token=public_token,
            payment_method=values.get("PaymentMethod"),
            operation_key=values.get("OpKey") or values.get("opKey"),
            event_digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def _refund_jwt(self, request: RobokassaRefundRequest) -> str:
        if not self.password3:
            raise RobokassaError("Robokassa Password3 is not configured")
        header = _b64url(_compact_json({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
        payload = _b64url(_compact_json({
            "OpKey": request.operation_key,
            "RefundSum": float(amount_text(request.amount_minor)),
            "InvoiceItems": [{
                "Name": request.item_name,
                "Quantity": 1,
                "Cost": float(amount_text(request.amount_minor)),
                "Tax": request.tax,
                "PaymentMethod": "full_payment",
                "PaymentObject": "service",
            }],
        }).encode("utf-8"))
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = _b64url(hmac.new(
            self.password3.encode("utf-8"), signing_input, hashlib.sha256
        ).digest())
        return f"{header}.{payload}.{signature}"

    def create_refund(self, request: RobokassaRefundRequest) -> RobokassaRefundResult:
        try:
            response = self.client.post(
                self.refund_url,
                content=self._refund_jwt(request),
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
        except httpx.HTTPError as exc:
            raise RobokassaError("Robokassa refund request failed") from exc
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise RobokassaError("Robokassa refund response was invalid") from exc
        accepted = bool(isinstance(payload, dict) and payload.get("success") is True)
        return RobokassaRefundResult(
            accepted=accepted,
            request_id=str(payload.get("requestId")) if accepted and payload.get("requestId") else None,
            error_code=(
                str(payload.get("message"))[:100]
                if isinstance(payload, dict) and payload.get("message") else None
            ),
            http_status=response.status_code,
        )

    def refund_status(self, request_id: str) -> tuple[str, int]:
        try:
            response = self.client.get(self.refund_status_url, params={"id": request_id})
        except httpx.HTTPError as exc:
            raise RobokassaError("Robokassa refund status request failed") from exc
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise RobokassaError("Robokassa refund status response was invalid") from exc
        label = str(payload.get("label", "unknown")) if isinstance(payload, dict) else "unknown"
        return label, response.status_code
