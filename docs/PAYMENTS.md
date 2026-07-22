# Pixora Payments

## Permanent v1 product

The only v1 payment product is `continuation_pack_2_plus_1` / Pixora Continuation Pack. Permanent user, site, offer and receipt copy: «Пакет Pixora: 2 варианта обработки и 1 оригинал — 49 ₽». There is no subscription or automatic renewal. A verified payment atomically grants two generation credits and one independent original entitlement. It never auto-unlocks a result.

The user may spend generation credits on Correction, Repeat, a ready scenario or a different photograph. The entitlement may later unlock one owned, existing, non-deleted GalleryVersion with a private original, whether created before or after purchase. Repeat purchases stack.

Real payments are fail-closed. Every deploy sets `PAYMENTS_ENABLED=false`, `PAYMENT_PROVIDER=disabled`, `PAYMENT_WEBHOOK_ENABLED=false`, `PAYMENT_REFUNDS_ENABLED=false`, `ROBOKASSA_MODE=sandbox` and `ROBOKASSA_PRODUCTION_APPROVED=false`. The independently controlled `PAYMENT_WEBHOOK_LISTENER_ENABLED=true` publishes only a loopback-backed readiness transport returning 503 while business callbacks are off. Enabling production requires a separate owner decision, credentials, HTTPS ResultURL, sandbox evidence, legal/fiscal review and a controlled real-payment smoke.

## Accounting boundaries

- initial grant: exactly two successful deliveries once per MAX user identity;
- generation: reserve one credit before provider, consume after delivered preview, release for provider/network/policy/storage/internal/MAX/cancel/restart failure;
- ResultURL: validate and commit +2 credits and +1 entitlement in one SQLite transaction;
- SuccessURL/browser redirect: display only, grants nothing;
- entitlement: atomically links to a selected version; missing original consumes nothing;
- original delivery: entitlement is reserved before MAX send and consumed only after
  successful delivery; failure returns it to available without another payment;
- duplicate callback/event/consumption/release: idempotent.

## Refund

A completely unused package may be rolled back atomically: its two available credits are removed and its available entitlement becomes refunded. If a generation credit or entitlement from that package was used or reserved, automatic rollback is rejected and the case requires manual review. A refund hold temporarily removes the specific package lot and entitlement from the spendable set. Payment evidence and audit are never deleted.

## Operator commands

```bash
python -m app.main credit-status --platform-user-id <MAX-ID>
python -m app.main credit-history --platform-user-id <MAX-ID>
python -m app.main entitlement-status --platform-user-id <MAX-ID>
python -m app.main entitlement-history --platform-user-id <MAX-ID>
python -m app.main package-status --platform-user-id <MAX-ID>
python -m app.main credit-adjust --platform-user-id <MAX-ID> --delta <N> --reason <ticket> --idempotency-key <key> [--apply]
python -m app.main entitlement-adjust --platform-user-id <MAX-ID> --delta <N> --reason <ticket> --idempotency-key <key> [--apply]
python -m app.main payment-show --invoice <invoice> --format human
python -m app.main payment-reconcile [--invoice <invoice>] --format human
python -m app.main refund-create --invoice <invoice> --amount-rub 49 --reason customer_request --idempotency-key <ticket> [--apply|--dry-run]
python -m app.main pilot-report --format human
python -m app.main cost-status --format human
```

Dangerous commands are dry-run by default and require explicit `--apply`, reason and idempotency key. Output masks user/order/version references and never prints credentials, signed URLs, MAX IDs, prompts or private paths.

## Evidence required before first real payment

- Robokassa merchant, Password1/2/3, receipt/tax settings and exact product description verified in the cabinet;
- public POST-only ResultURL behind trusted TLS and a loopback listener;
- sandbox evidence for successful +2/+1 grant, callback replay, wrong amount/signature, later version selection, original retry and refund review;
- offer/privacy/payment-refund text reviewed by qualified specialists;
- alerts, support owner, reconciliation and backup/restore evidence;
- explicit owner approval for the bounded real-money smoke.

Detailed procedures: [Robokassa](ROBOKASSA.md), [go-live audit](PAYMENT_GO_LIVE_AUDIT.md), [cabinet setup](ROBOKASSA_CABINET_SETUP.md), [sandbox E2E](ROBOKASSA_SANDBOX_E2E.md), [support](PAYMENT_SUPPORT_RUNBOOK.md).
