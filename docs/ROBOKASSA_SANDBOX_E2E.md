# Owner-only Robokassa sandbox E2E

Do not run this procedure without explicit owner approval and Robokassa test credentials. Snapshot DB and `.env`, keep a second SSH session open, restrict MAX to the owner, and cap the test to one 49 ₽ sandbox invoice. No real money is expected.

Before requesting that approval, all of these must be true: cabinet hash is SHA-256; test Password #1 and Password #2 are present without disclosure; `PAYMENT_RECEIPT_PAYMENT_METHOD` and `PAYMENT_RECEIPT_PAYMENT_OBJECT` are confirmed by Robokassa for Робочеки СМЗ; public GET is 405; disabled POST is 503; payments, refunds, pilot and public handlers remain off. A 503 readiness probe is not payment E2E evidence.

Common checks:

```bash
cd /opt/photo-bot
.venv/bin/python -m app.main robokassa-health --format human
.venv/bin/python -m app.main payment-show --invoice <INV> --format human
.venv/bin/python -m app.main payment-reconcile --invoice <INV> --format human
```

| # | Action / expected user message | Expected DB, audit, telemetry | Verification and PASS/FAIL |
|---|---|---|---|
| 1 | Owner receives a demo: «Готово — демо…» | succeeded version, demo quota decremented, `result_delivered` | Gallery shows exact version; fail if no original+preview |
| 2 | Owner taps «Получить оригинал» | `unlock_clicked`; no unlock yet | selected version ID is fixed; fail if sibling unlocks |
| 3 | Bot offers «Пакет Pixora: 2 варианта обработки и 1 оригинал — 49 ₽» | new pending order/intent/attempt/receipt, audit `order_created` | `payment-show`; name exact, quantity 1, cost/sum exactly 49.00 RUB and Receipt total equals OutSum |
| 4 | Bot sends sandbox payment URL | provider link contains `IsTest=1`, masked URL never logged | `payment-history`; fail if production URL lacks sandbox marker |
| 5 | Owner completes test payment | browser redirect may appear; DB may still be pending | do not treat browser page as success |
| 6 | Robokassa POSTs ResultURL | one callback event received | webhook log has request ID only; fail on GET acceptance |
| 7 | Signature/amount/invoice/token verify | callback event accepted | invalid mutation absent; response only `OK<InvId>` after commit |
| 8 | Bot confirms payment | order paid/delivery_pending, audit `payment_confirmed`, `payment_confirmed` telemetry | `payment-show`; fail if pending/failed |
| 9 | Exact selected version unlocks | selected version `unlocked`; siblings remain demo | DB lineage and ownership consistent |
| 10 | Original arrives in MAX | order delivered, delivery count +1, `original_delivered` | checksum/dimensions match stored original; fail on watermark |
| 11 | Replay identical callback | no second order/receipt/unlock; idempotent event | callback again; delivery count must not grow unexpectedly |
| 12 | Dry-run/resend original | dry-run no DB mutation; `--apply` adds delivery audit/count | `payment-resend-original --invoice <INV>` then approved `--apply` |
| 13 | Open SuccessURL directly | no user entitlement/state change | compare `payment-show` before/after; must be identical |
| 14 | Open FailURL directly | no paid state and no new order | compare DB/audit before/after |
| 15 | Simulate MAX delivery failure in controlled test | paid entitlement retained, status `delivery_pending`, error code/audit | `payment-mark-delivery-retry --invoice <INV>`; fail if paid is lost |
| 16 | Restart service after paid state | DB state survives; single PID after restart | `systemctl restart photo-bot`; `payment-show`; no duplicate delivery |
| 17 | Reconcile | zero local mismatches and same masked status in cabinet | `payment-reconcile --invoice <INV>`; manually compare provider status |
| 18 | Refund dry-run | no refund row/provider call | `refund-create --invoice <INV> --amount-rub 49 --reason customer_request --idempotency-key sandbox-<date> --dry-run`; DB unchanged |

## Required evidence file

After all 18 PASS results, create `data/robokassa_sandbox_evidence.json` on production (not Git) with only:

```json
{"verified": true, "mode": "sandbox", "verified_at": "<UTC>", "invoice_ref": "masked", "operator": "owner"}
```

Never include user ID, secrets, signed URL, full invoice, card/test-card data or callback body. On any FAIL: immediately restore `MAX_POLL_OBSERVE_ONLY=true`, `PAYMENTS_ENABLED=false`, `PAYMENT_WEBHOOK_ENABLED=false`, `PAYMENT_REFUNDS_ENABLED=false`, stop and investigate.
