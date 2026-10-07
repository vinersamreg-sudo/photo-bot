# Payments

## Products

- **49 ₽** one-time package: two edits and one original. Its public/fiscal
  name remains **Пакет доступа Ravuna** and its identifiers are unchanged.
- **1990 ₽** one-time package: 100 edits and 50 originals. Its fiscal item is
  **Пакет Ravuna: 100 обработок и 50 оригиналов**.
- Neither package is a subscription and neither creates recurring charges.
- Every PaymentOrder persists the exact package code, amount and grant
  quantities; reuse and expired-checkout recovery preserve all four values.

## Provider configuration

- Provider: Robokassa.
- Production mode uses SHA-256.
- Receipt item is package-specific, quantity 1, tax `none`, with exact sum
  49.00 or 1990.00.
- Existing fiscal fields and self-employed receipt configuration must not be
  changed without a separate fiscal review.
- Static SuccessURL and FailURL remain configured in the Robokassa cabinet.
- Each payment also sends signed dynamic `SuccessUrl2`/`FailUrl2` GET URLs that
  point to Ravuna's opaque-token browser return endpoints.

## Authoritative flow

1. Ravuna shows one payment offer for an explicit completed version, durable
   pending edit request or account top-up. Rendering the offer creates/reuses
   one target-scoped PaymentIntent and PaymentOrder.
2. The package-specific pay button contains only Ravuna's short opaque URL
   `https://ravuna.ru/p/<opaque-token>`; no long provider URL or intermediate
   “link ready” screen is shown.
3. The short endpoint resolves only an active owned order and returns an
   auto-submitted POST form to Robokassa. Provider parameters are hidden form
   fields, so the browser address bar does not contain the long signed query.
   An order is active/reusable only while its status is `pending` and
   `expires_at` is strictly in the future. The current checkout TTL remains 30
   minutes.
4. Robokassa POSTs ResultURL.
5. Ravuna verifies Password #2 signature, invoice, order state, package code,
   exact package amount and persisted grant quantities.
6. One transaction marks the order paid and grants exactly +2/+1 or +100/+50.
7. Exactly one sale receipt/audit record is associated with the payment.
8. The browser return opens Ravuna in MAX. A return before ResultURL shows
   “Проверяем оплату…” and does not grant or consume anything.

An already-created pending MAX checkout exposes “Проверить оплату”; the ordinary
purchase/zero-balance screen does not. It is a read-only lookup of the owned
PaymentOrder and current ledgers: paid shows current balances, pending
explains that provider confirmation has not arrived, and an expired checkout
offers the existing owner-verified refresh action. This check never calls grant
or delivery logic; ResultURL remains the only authoritative confirmation.

SuccessURL/FailURL browser handlers only read order state, append bounded journey
diagnostics and redirect to MAX.
They never confirm a payment, change order state, grant credits or consume an
entitlement. Confirmed `original_download` resumes the exact version and
delivers its original; confirmed `processing_request` restores the exact saved
prompt and one or two sources. Missing context fails closed without substituting
the latest work. A cancelled payment retains the pending context.

An expired `/p/<opaque-token>` request never creates a PaymentOrder from the
anonymous browser. It marks a still-pending order expired idempotently and shows
a bounded return link to MAX. MAX verifies the current platform user against the
original order owner before creating or reusing one fresh order with the same
package, purpose and exact version/pending-request/account context. A wrong user fails
closed. The old order remains expired, and only ResultURL can grant the package.

## Signature contracts

Outgoing payment signature uses the established canonical fields:

```text
MerchantLogin:OutSum:InvId:Receipt:SuccessUrl2:SuccessUrl2Method:FailUrl2:FailUrl2Method:Password1:Shp_order=...
```

`Receipt` is signed as the provider's once-encoded JSON value. ReturnURL
modifiers are signed as the exact raw URL values placed in the POST fields;
HTML form encoding is applied only by the transport afterwards. The signed
values and the POST fields must be built by the same request builder.

ResultURL verification uses the Robokassa callback contract and Password #2.
Refund operations, when separately enabled, use their dedicated credential and
contract. Never print canonical strings containing real passwords.

## Idempotency

- Duplicate button taps should reuse an applicable pending purchase.
- Every reuse path applies the same reusable-order predicate: `pending` status
  and `expires_at > now`. Status alone is insufficient.
- Pending purchases are target-scoped; an absent target must fail closed and
  must never fall back to the latest completed work.
- Duplicate/concurrent ResultURL callbacks return the expected acknowledgement
  without a second grant, entitlement, receipt or ledger mutation.
- PaymentIntent, provider order, grant and receipt have independent idempotency
  keys/constraints.
- Browser redirects cannot race the server callback into a grant.
- Restart does not lose paid state or entitlement.
- A valid signed ResultURL may arrive after the local checkout TTL; it remains
  authoritative and idempotent so received money cannot be left without the
  purchased package.

## Failure behavior

- Invalid signature/amount/invoice: reject and log a privacy-safe event.
- Webhook disabled: POST returns 503; GET remains 405.
- Payment pending: explain that confirmation may be delayed.
- Expired checkout: explain that the link is stale and return the owner to MAX
  to refresh it; never create a new order from an anonymous GET.
- Paid but original delivery failed: preserve entitlement and allow retry.
- Refund execution stays disabled unless explicitly approved.

### Payment journey diagnostics and recovery

Journey events use the existing private `payment_audit` table, with
`actor_type=journey`, UTC `created_at`, the existing order foreign key and a
bounded JSON `reason` (`telemetry_version=1`). No schema migration is required.
Events are `checkout_created`, `payment_link_opened`, `payment_link_reopened`,
`success_url_return`, `fail_url_return`, `max_payfail_return`,
`payment_status_checked`, and `checkout_created_after_failure`. The latter links
only an unpaid previous order for the same owner, product and exact target.
Existing accepted `payment_events/result_url`, webhook validation and package
grant records remain the authoritative payment evidence.

MAX event keys are hashed; replay does not duplicate the observation. Browser
returns are first-only per route/invoice because SuccessURL auto-refreshes while
waiting for confirmation. Link hits are counted separately from distinct
invoices: they may include browser previews and do not prove a provider attempt
or a visit to the bank. Journey writes are best effort and never confirm,
cancel, expire, grant or consume a purchase themselves. A diagnostic-write
failure must not block the existing checkout/return flow.

An active failed checkout in MAX offers “💳 Попробовать оплатить ещё раз” using
the same short URL/invoice, “✅ Проверить оплату”, then Back. Expired checkout
uses the existing owner-verified refresh flow, preserving product and target.
A late FailURL after a valid ResultURL resumes the confirmed exact context,
not a new purchase. Normal pending-status lookup still changes no payment or
ledger state; only its bounded click audit is added.

Do not store arbitrary `PaymentMethod`, PAN, bank details, email, request query,
signature or URLs. The current classic signature does not authenticate the
optional PaymentMethod field. Official verified method metadata requires a
separately supported source (for example verified ResultUrl2); this change does
not enable a new provider callback or alter merchant settings. Until then the
report labels PaymentMethod `NOT MEASURABLE`.

`scripts/ravuna payment-report --days 7 [--db SNAPSHOT] [--at ISO8601]
[--format human|json]` is invoice-cohort reporting: orders created in the rolling
Samara `[from,to)` window, outcomes known before cutoff. It deduplicates accepted
callbacks/grants, excludes proven configured MAX owners fail-closed, shows
recovery on the same or a new invoice, and counts no browser return as payment.
Stages are optional observations, not a mandatory linear conversion funnel.
Historical missing link/return observations cannot establish abandonment;
telemetry coverage is reported explicitly. It never calls Robokassa and opens
SQLite with `mode=ro`, `query_only=ON` in one consistent read transaction.

Aggregate reconciliation exposes `expired_checkout_events` and
`refreshed_checkout_events`; these metrics contain no user identifiers.

## Production flags

### Optional checkout method restriction

`ROBOKASSA_PAYMENT_METHODS=` (default) preserves the existing unrestricted form.
The intended opt-in configuration is `BankCard,SBP,SberPay`; each configured
alias becomes a separate repeated `PaymentMethods` POST field, not a combined
comma-separated provider value. The same setting applies to both 49 ₽ and
1990 ₽ orders, including reopening a reusable invoice. Configured order is
preserved in transport; Robokassa may arrange the visible methods differently.
This follows the official [payment interface](https://docs.robokassa.ru/ru/pay-interface)
contract (`PaymentMethods` uses aliases from GetCurrencies).

Only the reviewed case-sensitive aliases `BankCard`, `SBP`, `SberPay` are accepted.
Unknown aliases, duplicates and empty elements within a nonempty list fail
startup with a fixed safe error. This avoids silently ignoring an operator's
restriction or sending an invalid form. Whitespace around aliases is ignored.
An entirely blank setting deliberately means no restriction.

`PaymentMethods` is not included in the classic signature; the existing signed
fields, Receipt, return URLs and ResultURL-only grant contract are unchanged.
Merchant GetCurrencies availability is not proof of checkout rendering or bank
deep-link behavior. Restriction does not itself fix bank app redirects, and no
conversion improvement or visual ordering is guaranteed without controlled
post-review verification. No merchant settings are changed by this feature.

Restriction rollback is one configuration change: `ROBOKASSA_PAYMENT_METHODS=`
and the normal approved runtime configuration reload/restart procedure. It
does not revert orders or grants. Ordinary deploy still preserves `.env` and
does not activate this opt-in setting automatically.

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
