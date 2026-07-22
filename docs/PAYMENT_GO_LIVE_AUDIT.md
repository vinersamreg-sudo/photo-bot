# Payment go-live audit

Verdict: architecture and fail-closed ResultURL transport are materially ready for an owner-only sandbox, but **not ready to run that sandbox payment or accept production money**. The blocking gates are SHA-256 alignment in the cabinet, test credentials, confirmed Робочеки СМЗ `payment_method`/`payment_object`, sandbox evidence, real owner payment, delivery/restart verification, refund and reconciliation verification.

Legend: `PASS` verified by code/tests/read-only production evidence; `SANDBOX TEST REQUIRED`; `PRODUCTION TEST REQUIRED`; `OWNER ACTION`; `BLOCKED`.

| Control | Status | Evidence / remaining gate |
|---|---|---|
| PaymentIntent/Order/Attempt/Event/Audit/Receipt plus credit/entitlement schema | PASS | migrations v8/v9, SQLite constraints and test suite |
| RefundIntent/audit | PASS | prepare/preview/idempotency tests; provider execution remains off |
| Price source of truth | PASS | integer `UNLOCK_ORIGINAL_PRICE_RUB=49`, DB minor units 4900; no float money |
| Provider request/signature | PASS | SHA-256, Password1, server amount, opaque `Shp_order`, receipt |
| ResultURL signature | PASS | Password2 and timing-safe `hmac.compare_digest` |
| Exact amount/currency/invoice/merchant/provider | PASS | fail-closed callback transaction |
| Exact GalleryVersion and ownership | PASS | attempt, item and user lineage checked before unlock |
| Browser SuccessURL/FailURL cannot unlock | PASS | redirect endpoints are not payment state inputs |
| Idempotency/duplicate/replay | PASS | event digest, unique invoice/order/idempotency keys |
| Expired callback | PASS | rejected without paid state |
| Payment failure vs demo quota | PASS | quota is consumed by successful generation, not payment redirect |
| Delivery failure after paid | PASS | order remains `delivery_pending`, exact original remains recoverable |
| Manual resend and delivery retry | PASS | privacy-safe CLI; mutation requires `--apply`; audit trail |
| Refund validation/idempotency | PASS | dry-run does not mutate; amount cannot exceed remaining paid amount |
| Refund provider operation | SANDBOX TEST REQUIRED | Password3 and Robokassa response semantics must be proven |
| Local reconciliation | PASS | `payment-reconcile` checks ownership/version/receipt/unlock/file/audit |
| Provider-vs-local reconciliation | OWNER ACTION | compare masked invoice/status with cabinet; no provider status API wired |
| Webhook HTTP hardening | PASS | POST only, 64 KiB, strict UTF-8, wrong path/method rejected |
| Public HTTPS ResultURL transport | DEPLOY REQUIRED | Nginx and loopback listener are implemented; disabled POST must be verified as 503 in production before any cabinet edit |
| Hash alignment | OWNER ACTION | code uses SHA-256; cabinet was observed on MD5; no cabinet change made |
| Sandbox credentials and cabinet methods | OWNER ACTION | GitHub production environment has no Robokassa secrets; exact test Password1/2 presence in production must be verified without disclosure |
| Owner sandbox E2E | SANDBOX TEST REQUIRED | execute the 18-step runbook with explicit owner approval |
| Receipt name/amount/tax | PASS | exact package, quantity 1, integer minor units 4900 rendered as 49.00, `tax=none` for without VAT |
| Fiscal payment method/object | OWNER ACTION | code intentionally requires non-empty confirmed values before payments can enable |
| Production approval | BLOCKED | `ROBOKASSA_PRODUCTION_APPROVED=false` by design |
| First real owner payment | PRODUCTION TEST REQUIRED | only after all previous gates and separate approval |
| Production refund/reconciliation | PRODUCTION TEST REQUIRED | controlled owner transaction only |
| Secret/log redaction | PASS | operator output masks references; no signed URLs, paths or user IDs |

## Failure and money-loss analysis

- Provider confirms payment, MAX delivery fails: money is not lost; order becomes `delivery_pending`; support resends the stored exact version.
- Browser shows success but ResultURL never arrives: original remains locked; operator checks cabinet and local reconciliation before any manual action.
- Duplicate callback: same event is idempotent and must not create a second delivery/payment.
- DB commit fails: application must not return `OK<InvId>`; Robokassa can retry.
- Original file was deleted after payment: this is a support incident; do not mark delivered and do not silently substitute another version. Restore from backup or refund.
- Incorrect tax/receipt configuration: financial/legal blocker even when the technical payment succeeds.

## Go-live condition

Production payment readiness becomes true only after recorded sandbox evidence, provider approval, production credentials/mode, public ResultURL, verified receipt settings, enabled refund recovery and a separate owner authorization. Until then all payment flags remain false.
