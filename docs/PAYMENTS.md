# Pixora Payments

## Scope

Pixora sells access to the original of one exact `GalleryVersion`. A successful payment does not unlock the user, the gallery item, sibling versions, future corrections or repeats. Price and currency come from server configuration; the current product price is 49 RUB.

Real payments are fail-closed. Every deploy sets `PAYMENTS_ENABLED=false`, `PAYMENT_PROVIDER=disabled`, `PAYMENT_WEBHOOK_ENABLED=false`, `PAYMENT_REFUNDS_ENABLED=false`, `ROBOKASSA_MODE=sandbox` and `ROBOKASSA_PRODUCTION_APPROVED=false`. Enabling production requires a separate owner decision, credentials, HTTPS reverse proxy, provider sandbox evidence, legal/fiscal review and a controlled real-payment smoke.

## State machine

`pending → paid → delivered` is the normal path. If MAX delivery fails, the order becomes `delivery_pending`; the paid version stays unlocked and can be delivered later without a second payment. Expired and rejected callbacks never unlock content. Refund states are `draft → pending/processing → succeeded|failed|cancelled`; a confirmed full refund revokes further original access for that exact version without affecting siblings.

## Operator commands

```bash
python -m app.main payment-status --format human
python -m app.main payment-history [--invoice <invoice>] --format human
python -m app.main payment-show --invoice <invoice> --format human
python -m app.main payment-reconcile [--invoice <invoice>] --format human
python -m app.main payment-resend-original --invoice <invoice> [--apply]
python -m app.main payment-mark-delivery-retry --invoice <invoice> [--apply]
python -m app.main refund-create --invoice <invoice> --amount-rub 49 --reason customer_request --idempotency-key <ticket> [--apply|--dry-run]
python -m app.main refund-history [--refund-id <opaque-refund-id>]
python -m app.main refund-status --refund-id <opaque-refund-id> [--refresh]
python -m app.main robokassa-health --format human
```

Dangerous commands are dry-run by default and require explicit `--apply`. Reason is one controlled value: `customer_request`, `duplicate_payment`, `technical_failure`, `delivery_failure`, `quality_dispute` or `other`; free personal text is not stored. Provider refund/refresh remain blocked unless payment/refund flags and Password3 are explicitly enabled. Operator output masks references and does not print credentials, signed URLs, MAX IDs, prompts or private paths. `--format json` is available for machine-readable read-only reports.

Detailed procedures: [go-live audit](PAYMENT_GO_LIVE_AUDIT.md), [cabinet setup](ROBOKASSA_CABINET_SETUP.md), [sandbox E2E](ROBOKASSA_SANDBOX_E2E.md), [support](PAYMENT_SUPPORT_RUNBOOK.md).

## Evidence required before first real payment

- Robokassa merchant, Password1/2/3 and production hash settings are stored only in protected secrets;
- public ResultURL terminates TLS and proxies only to the loopback listener;
- sandbox payment, duplicate ResultURL, delayed original delivery and refund reconciliation are evidenced;
- receipt item name and tax code are approved by accountant/lawyer;
- offer, privacy, payment, failed-delivery and refund rules are published;
- alerts, support owner and manual reconciliation runbook exist;
- explicit production approval flag is set only for the bounded smoke and reviewed afterwards.
