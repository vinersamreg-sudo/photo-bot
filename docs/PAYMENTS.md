# Pixora Payments

## Scope

Pixora sells access to the original of one exact `GalleryVersion`. A successful payment does not unlock the user, the gallery item, sibling versions, future corrections or repeats. Price and currency come from server configuration; the current product price is 149 RUB.

Real payments are fail-closed. Every deploy sets `PAYMENTS_ENABLED=false`, `PAYMENT_PROVIDER=disabled`, `PAYMENT_WEBHOOK_ENABLED=false`, `PAYMENT_REFUNDS_ENABLED=false`, `ROBOKASSA_MODE=sandbox` and `ROBOKASSA_PRODUCTION_APPROVED=false`. Enabling production requires a separate owner decision, credentials, HTTPS reverse proxy, provider sandbox evidence, legal/fiscal review and a controlled real-payment smoke.

## State machine

`pending → paid → delivered` is the normal path. If MAX delivery fails, the order becomes `delivery_pending`; the paid version stays unlocked and can be delivered later without a second payment. Expired and rejected callbacks never unlock content. Refund states are `draft → pending/processing → succeeded|failed|cancelled`; a confirmed full refund revokes further original access for that exact version without affecting siblings.

## Operator commands

```bash
python -m app.main payment-status
python -m app.main payment-history --order-id <opaque-order-id>
python -m app.main refund-prepare --order-id <opaque-order-id> --amount-rub 149.00 --reason customer_request --idempotency-key <opaque-key>
python -m app.main refund-submit --refund-id <opaque-refund-id>
python -m app.main refund-status --refund-id <opaque-refund-id> [--refresh]
python -m app.main refund-history --refund-id <opaque-refund-id>
```

`refund-prepare` is local and safe. Reason is one controlled value: `customer_request`, `duplicate_payment`, `technical_failure`, `delivery_failure`, `quality_dispute` or `other`; free personal text is not stored. `refund-submit` and provider refresh remain blocked unless payment/refund flags and Password3 are explicitly enabled. Commands do not print credentials, MAX IDs, prompts or private paths.

## Evidence required before first real payment

- Robokassa merchant, Password1/2/3 and production hash settings are stored only in protected secrets;
- public ResultURL terminates TLS and proxies only to the loopback listener;
- sandbox payment, duplicate ResultURL, delayed original delivery and refund reconciliation are evidenced;
- receipt item name and tax code are approved by accountant/lawyer;
- offer, privacy, payment, failed-delivery and refund rules are published;
- alerts, support owner and manual reconciliation runbook exist;
- explicit production approval flag is set only for the bounded smoke and reviewed afterwards.
