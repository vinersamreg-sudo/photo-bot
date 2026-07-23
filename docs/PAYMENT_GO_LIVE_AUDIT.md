# Payment go-live audit

Verdict after the written Robokassa support decision: the former fiscal-field blocker is resolved and code is ready for a separately authorized owner-only sandbox E2E. Payments are still disabled and production money is not authorized.

| Control | Status | Evidence / remaining gate |
|---|---|---|
| Product and price | PASS | one «Пакет доступа Pixora», 49 ₽, +2 processing credits and +1 original entitlement |
| Receipt shape | PASS | one item; name, quantity 1, sum 49.00, tax none |
| NPD/Робочеки СМЗ fields | PASS | written support answer permits omission of `sno`, `payment_method`, `payment_object` |
| One-check sale model | PASS | one payment receipt row; processing/original/redelivery add no sale receipt |
| Receipt money | PASS | integer 4900 source, exact 49.00 render and OutSum equality |
| GET encoding | PASS | once-encoded Receipt signed, twice-encoded query value, Cyrillic recovery tested |
| Password #1 signature | PASS | SHA-256, Receipt and actual URL2/Shp modifiers included |
| ResultURL | PASS | Password #2, invoice, amount, merchant/provider and token validation |
| Duplicate callback | PASS | one grant and one sale receipt |
| Browser redirects | PASS | no grant or payment status mutation |
| Fail-closed public transport | PASS | production GET 405, disabled POST 503 must remain verified after deploy |
| SQLite/recovery | PASS | migrations, quick_check and stale-processing audit |
| Cabinet SHA-256 | OWNER ACTION | must be confirmed in the cabinet before sandbox |
| Test MerchantLogin/Password #1/#2 | OWNER ACTION | secrets required without disclosure |
| Owner-only sandbox E2E | SEPARATE APPROVAL REQUIRED | one 49 ₽ test invoice only |
| Production approval | BLOCKED | remains false until sandbox and separate owner decision |
| Real payment/refund | NOT TESTED | no authorization |

## Remaining steps before the sandbox

1. Owner confirms the cabinet hash is SHA-256.
2. Owner installs test MerchantLogin, Password #1 and Password #2 in GitHub production secrets or production `.env`.
3. Owner explicitly authorizes one owner-only sandbox transaction.

Password #3 and refund execution are not required for the sale sandbox; test them only under a separate approval.

## Production go-live

Requires recorded sandbox evidence, reconciliation, support/rollback readiness, qualified legal/fiscal review, production credentials and a separate authorization for the first real owner payment. Do not infer production readiness from unit tests.
