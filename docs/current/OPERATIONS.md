# Operations

## Compact status

On production, use the read-only summary first:

```bash
cd /opt/photo-bot
venv/bin/python scripts/production_status.py --root /opt/photo-bot
```

It reports SHA, systemd/runtime/restarts, health, SQLite, processing, orphan state
and non-secret operating flags. It must not mutate `.env`, SQLite or storage.

## Built-in reports

```bash
venv/bin/python -m app.main health
venv/bin/python -m app.main launch-status
venv/bin/python -m app.main health-report --format human
venv/bin/python -m app.main payment-status --format human
venv/bin/python -m app.main robokassa-health --format human
venv/bin/python -m app.main storage-status --format human
venv/bin/python -m app.main backup-status --format human
venv/bin/python -m app.main monitoring-status --format human
venv/bin/python -m app.main maintenance-cleanup
```

`maintenance-cleanup` and provider/storage cleanup commands are dry-run unless an
explicit execute/apply flag is supplied.

## Daily checks

- service active and one runtime;
- restart count and recent error tail stable;
- health and SQLite quick check green;
- no stale processing;
- orphan count reviewed;
- free disk above threshold;
- backup fresh, restore/off-site evidence current;
- MAX poll contact fresh;
- OpenAI balance confirmation current enough for the configured guard;
- payment callback/grant/delivery mismatch count zero;
- operating flags match approved production baseline.

## Incident triage

1. Preserve current state and timestamp.
2. Read the compact status and relevant privacy-safe report.
3. Inspect only the relevant log tail; do not paste full logs.
4. Determine whether the failure is provider, MAX delivery, state, storage,
   payment callback or host infrastructure.
5. Use the smallest reversible containment action.
6. Preserve payment and entitlement evidence before retries.
7. Restore exact pre-test state after any diagnostic mutation.

## Payment support

Use masked operator commands and dry-run first:

```bash
venv/bin/python -m app.main payment-show --invoice <invoice> --format human
venv/bin/python -m app.main payment-reconcile --invoice <invoice> --format human
venv/bin/python -m app.main payment-resend-original --invoice <invoice>
venv/bin/python -m app.main payment-mark-delivery-retry --invoice <invoice>
```

Apply/retry only with explicit authority and an operator ticket/idempotency key.
Never grant manually before reconciling the callback and ledgers.

## Provider incidents

- If OpenAI is slow, keep the user’s job state durable and communicate delay.
- If budget guard stops image requests, MAX must remain responsive with a clear
  unavailable message.
- Never convert a provider failure into a successful debit.
- Do not use a real image request as a health probe.

## Host incidents

- systemd is the only runtime manager; no nohup/cron/watchdog duplicates.
- Start/stop through the scoped scripts/systemd procedures.
- After restart verify SQLite, processing, MAX poll freshness and payment recovery.
- VPS loss requires infrastructure restore plus encrypted backup restoration;
  source redeploy alone is not data recovery.

## Output safety

Operator reports and support notes must not contain platform user IDs, tokens,
passwords, prompts, photos or private file paths. Use masked invoice/order refs.
