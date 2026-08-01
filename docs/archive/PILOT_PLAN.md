# Closed Pilot Plan

Exact activation, rollback, budget and tester copy are in [PILOT_5_USERS_RUNBOOK.md](PILOT_5_USERS_RUNBOOK.md). Unit economics and its executed notebook are in [PILOT_UNIT_ECONOMICS.md](PILOT_UNIT_ECONOMICS.md) and `analysis/PILOT_UNIT_ECONOMICS.ipynb`.

## Owner gate

Five full E2E successes in a row; no stuck status, original leak, quota debit on technical failure or lost GalleryVersion; encrypted backup restored; cleanup and recovery verified. Real image requests are agreed before execution and counted.

Owner image gate is complete. Before external users, deploy migration v9 with payments off, create/restore/off-site a fresh backup, verify no payment listener is public and complete owner sandbox separately. A five-user generation pilot does not require real payments. Measure the permanent two-generation global balance, package click intent and exhaustion UX; do not grant a second free pack through new photos or `/start`.

## Five users

Activate the first five entries of secret `MAX_PILOT_USER_IDS`; do not reveal IDs in reports. Review launch-status daily. Track start → upload → prompt → processing → delivery, first-result completion, corrections/repeats, feedback, errors, latency and estimate. Provide manual support.

Keep `PAYMENTS_ENABLED=false` unless the owner explicitly starts the separate payment rollout. The closed-test message must remain the only response for users outside owner + active five-ID prefix, with no OpenAI request and no order creation.

## Ten users

Proceed only with first-result completion ≥80%, delivery failure <5%, no technical debit and no critical data loss. Reconcile estimated and billed cost; document negative feedback causes and recovery drill.

## Twenty users

Proceed only after real feedback, correction-depth and unit-economics evidence, plus a decision on payment/legal/support. Then consider deterministic parser rules. Semantic parser or second provider requires a measured problem, not intuition.

## Stop conditions

Any original disclosure, payment/data loss, repeated stuck processing, failed restore, uncontrolled spend, policy/abuse incident or unexplained delivery failure returns production to observe-only and pauses invitations.
