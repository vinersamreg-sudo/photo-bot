# Ravuna production readiness gates

## OpenAI budget control

Ravuna does not scrape the OpenAI dashboard and never labels a manual number as live. The owner records the last dashboard-confirmed balance:

```bash
python -m scripts.openai_balance_confirm \
  --env-file /opt/photo-bot/.env \
  --balance-usd <dashboard amount>
python -m app.main openai-budget-status --format human
```

The record stores `OPENAI_BALANCE_USD` and `OPENAI_BALANCE_CONFIRMED_AT`. `OPENAI_BALANCE_WARNING_USD` defaults to 5, `OPENAI_BALANCE_CRITICAL_USD` to 1, and confirmations older than `OPENAI_BALANCE_MAX_AGE_HOURS` are stale. At or below the critical threshold, or when `OPENAI_IMAGE_REQUESTS_ENABLED=false`, new image requests stop before the provider call while MAX keeps running and shows the existing provider-unavailable message.

Daily safeguards remain `DEMO_DAILY_GENERATION_LIMIT`, `DEMO_DAILY_COST_LIMIT_RUB` and `DEMO_ESTIMATED_COST_RUB_PER_GENERATION`.

## Monitoring

`python -m app.main monitoring-status --format human` is the minimal privacy-safe operator channel.

P0 signals: process down, stale polling, SQLite failure, rejected payment callback, paid order without a grant, paid order awaiting original retry, globally disabled image requests, critically low disk.

P1 signals: low or stale manually confirmed OpenAI balance, repeated provider/MAX delivery failures, pending-intent mismatch, stale processing, stale backup.

No photo, prompt, MAX ID, secret or private path is emitted. An external pager is not configured; until it exists the owner must run the report before opening the pilot and at least daily during the pilot.

## Exact state restoration

Approved test workflows snapshot the complete runtime `.env` with mode 600 before mutation. After the test they restore the exact file and compare all protected state:

- MAX observe-only and pilot allowlist state;
- payment/provider/webhook/refund/Robokassa mode and approval;
- pilot limit;
- OpenAI image emergency stop;
- sandbox probe state.

If exact restore fails, the workflow applies fail-closed flags, stops image requests, reports the failure and exits non-zero. It must never silently replace a working production state with a generic test default.

## Five-user pilot target

The target is prepared but not enabled. A separate owner authorization is required for:

- `MAX_POLL_OBSERVE_ONLY=false`;
- exactly five IDs in `MAX_PILOT_USER_IDS`;
- `PILOT_USER_LIMIT=5`;
- payments/provider/webhook in explicitly approved production mode;
- refunds disabled.

Owner is separate and does not consume one of the five pilot places. Outsiders continue to receive the short closed-test response.
