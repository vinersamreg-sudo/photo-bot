# Production

This is the single source of truth for the approved production operating state.
Do not copy these flags into other current documents.

## Topology

- Hetzner VPS, Ubuntu 24.04 LTS.
- Application: `/opt/photo-bot`.
- Runtime/deploy user: `photoapp`.
- Python: `/opt/photo-bot/venv/bin/python`.
- Service: `photo-bot.service`, exactly one runtime.
- Configuration: `/opt/photo-bot/.env`, mode `600`.
- Mutable state: `data/`, `logs/`, `temp/`.
- Static site: `/opt/ravuna-site/current` behind nginx/HTTPS.
- Public domain: `https://ravuna.ru` and canonical apex redirect.

## Approved normal baseline

At the time of this efficiency sprint, public production is intentionally open:

```text
MAX_PUBLIC_ACCESS_ENABLED=true
MAX_POLL_OBSERVE_ONLY=false
MAX_TRANSPORT_MODE=polling
PAYMENTS_ENABLED=true
PAYMENT_PROVIDER=robokassa
PAYMENT_WEBHOOK_ENABLED=true
PAYMENT_REFUNDS_ENABLED=false
ROBOKASSA_MODE=production
ROBOKASSA_PRODUCTION_APPROVED=true
OPENAI_IMAGE_REQUESTS_ENABLED=true
PILOT_USER_LIMIT=0
```

This is a documented baseline, not permission to rewrite `.env`. Always read and
snapshot the actual current values before testing or deployment.

## Fresh-install baseline

`.env.example` and missing-key deploy defaults intentionally remain fail-closed:

```text
MAX_PUBLIC_ACCESS_ENABLED=false
MAX_POLL_OBSERVE_ONLY=true
PAYMENTS_ENABLED=false
PAYMENT_PROVIDER=disabled
PAYMENT_WEBHOOK_ENABLED=false
PAYMENT_REFUNDS_ENABLED=false
ROBOKASSA_MODE=sandbox
ROBOKASSA_PRODUCTION_APPROVED=false
PILOT_USER_LIMIT=0
```

`OPENAI_IMAGE_REQUESTS_ENABLED=true` is not sufficient to make a fresh service
public; transport/access/payment gates still deny public commercial operation.

## Ordinary deploy rule

An ordinary deploy preserves the existing values in `.env`. It may set a safe
default only when a key is missing. It must not close a working public service and
must not open a fresh server.

## Required health evidence

- deployed SHA matches the intended GitHub SHA;
- systemd active, one MainPID/runtime, bounded restart count;
- `python -m app.main health` passes;
- SQLite `quick_check=ok`;
- processing count is zero outside active jobs;
- orphan count is zero or a reviewed dry-run finding;
- public/observe/payment/provider/webhook/mode flags match the pre-deploy snapshot;
- ResultURL GET is 405; disabled POST is 503, enabled POST is not probed with a
  fake signed payment;
- disk, backup and logs are healthy.

## Emergency controls

Emergency closure is a deliberate incident action, not a normal deploy default.
Record the pre-incident state, reason, operator and restoration plan. Typical
guards are observe-only/public access, payment/webhook and image-request flags.
Never disable ResultURL while genuine callbacks may still be in flight without an
explicit payment incident decision.

## State restoration

Temporary E2E workflows must snapshot and restore the exact environment. Protected
state includes public access, observe-only, payments/provider/webhook/refunds,
Robokassa mode/approval, pilot controls and OpenAI image request guard. A mismatch
is a failed test and requires operator attention.

See [Deploy](DEPLOY.md) and [Operations](OPERATIONS.md).
