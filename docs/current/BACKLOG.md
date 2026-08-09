# Backlog

Only known, evidence-backed work is listed here. Priorities describe product risk,
not authorization to implement.

## P0

- No open confirmed P0 is recorded after the current public/payment launch.
- Any future loss of payment idempotency, original access control, production data,
  or public service availability becomes P0 immediately.

## P1

### Production monitoring and recovery

- Select and approve an external alert destination, configure
  `RAVUNA_ALERT_WEBHOOK_URL`, then perform one bounded alert-delivery test.
- Periodically exercise the encrypted recovery bundle on an isolated host and
  retain off-site evidence; synthetic local restore is the minimum recurring gate.

### Commercial reconciliation

- Continue comparing Robokassa cabinet payouts with the aggregate internal
  `payment-reconciliation` report; payout data remains an external owner check.
- Record actual variable costs and refund/support reserve for reliable margin.
- Keep delayed-callback and failed-original-delivery recovery evidence current.

### Image quality

- Measure instruction-following and identity preservation on approved synthetic
  group-photo cases.
- Add regression cases for multi-person clothing/background requests.
- Validate corrections/repeats against the selected parent version.

## P2

### Abuse and capacity

- Measure public rate-limit pressure and rejected/duplicate attempts.
- Define thresholds for moving beyond SQLite/single-process generation capacity.
- Add queue/object-storage infrastructure only after measured contention.

### Product analytics

- Track privacy-safe funnel completion, payment intent conversion and delivery
  failure without storing arbitrary prompts or image data in analytics.
- Reassess parser expansion only from real fallback/quality evidence.

### Content Studio

- Extend backup/restore coverage to Content Studio state before enabling publish.
- Use only rights-cleared Ravuna-owned demonstration assets.
- Keep external publishing disabled until a separate owner decision.

## P3

### Optional platform work

- Evaluate provider conversation memory only through bounded visual comparison.
- Reconsider second provider, semantic parser, advanced search and richer gallery
  UI only after measured customer demand.
- Review broader automation/admin tooling only when manual operations no longer
  fit the observed user volume.

Historical roadmaps and completed launch gates are in `docs/archive/`.
