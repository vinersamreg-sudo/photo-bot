# Pixora Payments

## Product

Pixora sells one digital service: «Пакет доступа Pixora» for 49 ₽. It includes two photo processing operations and one original without a watermark. There is no subscription or automatic renewal. A verified ResultURL atomically grants two internal credits and one independent original entitlement; it never auto-selects a version.

## Fiscal model

The mandatory single-item Receipt is:

```json
{"items":[{"name":"Пакет доступа Pixora","quantity":1,"sum":49.00,"tax":"none"}]}
```

This is the final model confirmed in writing by Robokassa support for a self-employed merchant using active «Робочеки СМЗ». `sno`, `payment_method` and `payment_object` are omitted. `PRICE_MINOR=4900` is the money source; rendered item sum and `OutSum` are both 49.00. See [the support decision](ROBOKASSA_SUPPORT_DECISION.md).

One confirmed payment creates one sale receipt. Package use and original delivery do not create more sale receipts. Do not reintroduce a two-check model without a new direct written requirement from Robokassa or the FNS.

## Transaction boundaries

- initial free access: exactly two successful deliveries once per MAX identity;
- processing: reserve one credit before provider, consume only after preview delivery, release on every failure;
- ResultURL: verify SHA-256 Password #2, invoice, amount and ownership, then commit +2/+1 once;
- browser redirects: informational only;
- original: reserve entitlement before MAX send, consume after delivery, release on failure;
- duplicate callback, reservation, consumption, release and delivery: idempotent;
- refund: completely unused package may be rolled back; used value requires review.

## Safe deployment

Every normal deploy keeps `MAX_POLL_OBSERVE_ONLY=true`, `PILOT_USER_LIMIT=0`, `PAYMENTS_ENABLED=false`, `PAYMENT_PROVIDER=disabled`, `PAYMENT_WEBHOOK_ENABLED=false`, `PAYMENT_REFUNDS_ENABLED=false`, `ROBOKASSA_MODE=sandbox` and production approval false. The listener may remain active only as a fail-closed 405/503 readiness transport.

## Operator commands

```bash
python -m app.main robokassa-health --format human
python -m app.main payment-show --invoice <invoice> --format human
python -m app.main payment-reconcile [--invoice <invoice>] --format human
python -m app.main payment-status --format human
python -m app.main launch-status
```

These commands do not create payments. Mutating commercial commands remain dry-run-first and require explicit `--apply`, reason and idempotency key.

Before the first sandbox transaction: align the cabinet to SHA-256, provide test MerchantLogin/Password #1/Password #2, verify the URLs and explicitly authorize one owner-only sandbox payment. Password #3 is needed only for a separately approved Refund API test.
