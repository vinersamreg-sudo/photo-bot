# Ravuna Payments

## Product

Ravuna sells one digital service: «Пакет доступа Ravuna» for 49 ₽. It includes two photo processing operations and one original without a watermark. There is no subscription or automatic renewal. A verified ResultURL atomically grants two internal credits and one independent original entitlement; it never auto-selects a version.

## Fiscal model

The mandatory single-item Receipt is:

```json
{"items":[{"name":"Пакет доступа Ravuna","quantity":1,"sum":49.00,"tax":"none"}]}
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
python -m app.main payment-expiration-reconcile --format human
python -m app.main payment-expiration-reconcile --apply --format human
python -m app.main payment-status --format human
python -m app.main launch-status
```

These commands do not create payments. Mutating commercial commands remain dry-run-first and require explicit `--apply`, reason and idempotency key.

`payment-expiration-reconcile` is narrower than commercial reconciliation: it changes only a pending PaymentIntent whose linked order is already `expired`, writes one privacy-safe audit event, and is idempotent.

## Production secret names

GitHub Environment `production` must contain `ROBOKASSA_MERCHANT_LOGIN`, `ROBOKASSA_PASSWORD_1` and `ROBOKASSA_PASSWORD_2`. The deploy maps them through stdin to runtime names `ROBOKASSA_MERCHANT_LOGIN`, `ROBOKASSA_PASSWORD1` and `ROBOKASSA_PASSWORD2`; values never appear in command arguments or reports and `/opt/photo-bot/.env` remains mode 600.

Take Password #1 and Password #2 from the production technical settings of the active Robokassa shop. Do not put test passwords into these names. After sandbox evidence is complete, remove `ROBOKASSA_TEST_PASSWORD_1` and `ROBOKASSA_TEST_PASSWORD_2` from the GitHub production environment. Presence is verified only as a boolean; values must never be printed.

Before the first sandbox transaction: align the cabinet to SHA-256, provide test MerchantLogin/Password #1/Password #2, verify the URLs and explicitly authorize one owner-only sandbox payment. Password #3 is needed only for a separately approved Refund API test.
