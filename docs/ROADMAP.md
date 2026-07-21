# Roadmap

## Immediate commercial gates — 20.07.2026

1. Owner submits the already published site package for Robokassa moderation.
2. After approval/test credentials, confirm fiscal settings and activate only owner/sandbox ResultURL for the 18-step E2E.
3. Reconcile exact version delivery, restart, duplicate callback and refund dry-run; store redacted sandbox evidence.
4. Separately run a no-payment five-user pilot and measure real generation count, conversion intent, latency, feedback and cost.
5. Do not enable real 49 ₽ payments until owner production approval, controlled real owner payment, refund and reconciliation all pass.

## Content Studio closed-mode gate — 21.07.2026

Core, renderer, queue, review, planning and dry-run MAX adapter are implemented.
Before importing the first real demonstration pair: extend encrypted backup to the
separate Content Studio SQLite and storage, prove restore/off-site copy, select one
rights-cleared Pixora-owned case, run visual/operator review, and keep network
publishing disabled. Enabling real MAX publication remains a later, separate owner
decision after a manual dry-run comparison.

## Gate 0 — owner

Owner image E2E gate выполнен. Deploy commercial candidate observe-only, применить migration v9, проверить credit/entitlement migration report, создать свежий encrypted backup + restore + off-site artifact, выполнить cleanup и проверить `health-report`. Затем отдельно настроить HTTPS ResultURL и пройти owner Robokassa sandbox без реальных денег.

Exit: five consecutive successful full E2E, no stuck processing/original leak/data loss, errors understandable, backup and cleanup proven.

## Gate 1 — 5 users

Enable exactly five IDs from secret allowlist. Payments можно оставить выключенными. Measure first-result completion, latency, error/delivery failure, parser fallback, feedback, corrections and estimated cost. Provide manual support and daily health/launch review.

Exit toward 10: completion ≥80%, delivery failure <5%, no debit on technical failure, no critical data loss, latency expectation understood.

## Gate 2 — 10 users

Validate actual OpenAI invoice against estimate, identify top negative-feedback causes, test recovery drill and support response. Завершить Robokassa sandbox, legal/fiscal review и только отдельным решением провести первый bounded real payment.

## Gate 3 — 20 users

Confirm demand, quality, correction depth and unit economics. Only then reassess semantic parser, second image provider, queue/object storage or webhook investments.

## Public launch

Blocked by verified payment/refund reconciliation, final legal/operator/fiscal details, support, abuse controls and evidence from prior gates. The MAX deep link and site implementation exist, but public HTTPS additionally requires DNS apex/www → `116.203.24.102`, trusted TLS, external smoke and only then enabling the gated site workflow. No multi-provider, Redis/Celery, admin panel or large asset catalog before measured need.

## Optional provider-memory gate

До pilot 5 выполнить отдельное owner-only сравнение максимум на 4 approved image
requests. Memory включать в пилоте только если corrections визуально лучше,
provider errors не растут, fallback доказан, а latency/cost приемлемы. Наличие
response ID само по себе не является успехом.
