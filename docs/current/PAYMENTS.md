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
- Static SuccessURL and FailURL remain configured in the Robokassa cabinet.
- Each payment also sends signed dynamic `SuccessUrl2`/`FailUrl2` GET URLs that
  point to Ravuna's opaque-token browser return endpoints.

## Authoritative flow

1. Ravuna shows one payment offer for an explicit completed version, durable
   pending edit request or account top-up. Rendering the offer creates/reuses
   one target-scoped PaymentIntent and PaymentOrder.
2. The “Оплатить 49 ₽” button contains only Ravuna's short opaque URL
   `https://ravuna.ru/p/<opaque-token>`; no long provider URL or intermediate
   “link ready” screen is shown.
3. The short endpoint resolves only an active owned order and redirects the
   browser to its freshly signed Robokassa URL.
4. Robokassa POSTs ResultURL.
5. Ravuna verifies Password #2 signature, amount, invoice and order state.
6. One transaction marks the order paid and grants +2 edits and +1 original.
7. Exactly one sale receipt/audit record is associated with the payment.
8. The browser return opens Ravuna in MAX. A return before ResultURL shows
   “Проверяем оплату…” and does not grant or consume anything.

SuccessURL/FailURL browser handlers only read order state and redirect to MAX.
They never confirm a payment, change order state, grant credits or consume an
entitlement. Confirmed `original_download` resumes the exact version and
delivers its original; confirmed `processing_request` restores the exact saved
prompt and one or two sources. Missing context fails closed without substituting
the latest work. A cancelled payment retains the pending context.

## Signature contracts

Outgoing payment signature uses the established canonical fields:

```text
MerchantLogin:OutSum:InvId:Receipt:SuccessUrl2:SuccessUrl2Method:FailUrl2:FailUrl2Method:Password1:Shp_order=...
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
