# Payments

## Product

- Public name: **Пакет доступа Ravuna**.
- Price: **49 ₽**.
- Grant: **two edits and one original**.
- Internal product/ledger identifiers remain stable for compatibility.

## Provider configuration

- Provider: Robokassa.
- Production mode uses SHA-256.
- Receipt item: “Пакет доступа Ravuna”, quantity 1, sum 49.00, tax `none`.
- Existing fiscal fields and self-employed receipt configuration must not be
  changed without a separate fiscal review.
- Static SuccessURL and FailURL are configured in the Robokassa cabinet.
- Dynamic `SuccessUrl2`/`FailUrl2` fields are not sent by the payment builder.

## Authoritative flow

1. Ravuna first shows a payment offer for an explicit completed version or
   durable pending edit request; no PaymentIntent exists before the pay click.
2. On “Оплатить 49 ₽”, Ravuna creates/reuses one bounded PaymentIntent and
   PaymentOrder for that exact target, then shows the signed Robokassa URL.
3. Robokassa POSTs ResultURL.
4. Ravuna verifies Password #2 signature, amount, invoice and order state.
5. One transaction marks the order paid and grants +2 edits and +1 original.
6. Exactly one sale receipt/audit record is associated with the payment.
7. The user may consume the original entitlement for an owned version.

SuccessURL/FailURL are informational browser pages only. They never call the
backend, change payment status, grant credits or consume entitlement.

## Signature contracts

Outgoing payment signature uses the established canonical fields:

```text
MerchantLogin:OutSum:InvId:Receipt:Password1:Shp_order=...
```

ResultURL verification uses the Robokassa callback contract and Password #2.
Refund operations, when separately enabled, use their dedicated credential and
contract. Never print canonical strings containing real passwords.

## Idempotency

- Duplicate button taps should reuse an applicable pending purchase.
- Pending purchases are target-scoped; an absent target must fail closed and
  must never fall back to the latest completed work.
- Duplicate ResultURL callbacks return the expected acknowledgement without a
  second grant, entitlement, receipt or ledger mutation.
- PaymentIntent, provider order, grant and receipt have independent idempotency
  keys/constraints.
- Browser redirects cannot race the server callback into a grant.
- Restart does not lose paid state or entitlement.

## Failure behavior

- Invalid signature/amount/invoice: reject and log a privacy-safe event.
- Webhook disabled: POST returns 503; GET remains 405.
- Payment pending: explain that confirmation may be delayed.
- Paid but original delivery failed: preserve entitlement and allow retry.
- Refund execution stays disabled unless explicitly approved.

## Production flags

The current commercial baseline is documented only in
[PRODUCTION.md](PRODUCTION.md). `.env.example` remains fail-closed.

## Verification

```powershell
python scripts/test_fast.py payments
python -m unittest -q tests.test_payments
python -m unittest -q tests.test_commerce
```

Real/sandbox payment E2E is T4: explicit authority, exactly bounded payment count,
pre-snapshot and exact restoration are mandatory.
