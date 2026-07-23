# Payment Architecture

## Components

- `RobokassaProvider`: deterministic GET payment link, canonical Receipt and SHA-256 signatures.
- `PaymentService`: durable order/event/webhook/receipt/audit state and atomic +2/+1 grant.
- `CommerceService`: credit lots, reservations, entitlements, rollback and admin audit.
- `PaymentWebhookServer`: loopback POST ResultURL behind Nginx.
- `MaxApplication`: package UX and later user-selected original.

## Sale boundary

Order creation writes exactly one `payment_receipts` row with `receipt_type=payment`, item «Пакет доступа Pixora», quantity 1, amount 4900 and tax `none`. Legacy `payment_method` and `payment_object` columns remain in SQLite for schema compatibility but new sale rows store empty values and the provider never serializes those fields.

The provider renders the canonical Receipt:

`{"items":[{"name":"Пакет доступа Pixora","quantity":1,"sum":49.00,"tax":"none"}]}`

The once-encoded Receipt is signed. The GET query contains its twice-encoded form.

## Confirmation boundary

ResultURL validates Password #2, invoice, amount, merchant/provider binding, currency and opaque token. One SQLite transaction:

1. records the callback;
2. marks order/intent paid;
3. marks the existing sale-receipt audit row confirmed;
4. creates one `continuation_pack_grant`;
5. adds two credits and one entitlement.

Only then is `OK<InvId>` returned. Unique constraints and event digests make callback replay idempotent. SuccessURL and FailURL do not enter this boundary.

## Use of the paid package

Processing uses the existing credit ledger. Original selection uses the entitlement ledger. Neither path calls a fiscal provider nor inserts another `receipt_type=payment` row. Original redelivery is also receipt-free. This is one paid package being consumed, not a second sale.

## Refund boundary

Refund preparation and audit remain separate and disabled. A `receipt_type=refund` row, if a separately approved refund succeeds, is return audit and cannot be counted as a second sale receipt. The existing sale row is never duplicated.

## Privacy and retention

Financial audit contains internal IDs, bounded enums and hashes—not images, prompts, MAX IDs, signed URLs or credentials. Historical schema columns are retained for old test/production records.
