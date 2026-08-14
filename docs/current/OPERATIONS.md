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
venv/bin/python -m app.main payment-reconciliation --format human
venv/bin/python -m app.main monitoring-status --format human
venv/bin/python -m app.main maintenance-cleanup
```

`maintenance-cleanup` and provider/storage cleanup commands are dry-run unless an
explicit execute/apply flag is supplied.

## External watchdog

The GitHub Actions workflow schedules the watchdog every ten minutes, but GitHub
only runs a scheduled workflow after that workflow exists in the default branch
`main`. The same compact detect-only check can be run on the VPS:

```bash
venv/bin/python -m scripts.production_watchdog --format human
```

Exit codes are `0` healthy, `1` warning and `2` critical. Output contains only
aggregate states. An optional HTTPS hook uses `RAVUNA_ALERT_WEBHOOK_URL`; without
it the report says `alert_transport=not_configured`. Do not enable `--notify`
until the owner approves a destination and a real alert test.

The scheduled command uses `--auto-heal --notify`. Auto-heal is still fail-closed:
it may run one `systemctl restart photo-bot.service` only for an inactive service,
stale polling while MAX API is reachable, or an unexpected application runtime
process while SQLite/migration/disk/network checks remain healthy. MAX, Robokassa
or network outages, payment/configuration errors, SQLite failures and migration
mismatches are alert-only. A state file under `data/` enforces a 30-minute
cooldown; a failed restart is never followed by a second automatic restart.
Payment preparation failures are recorded only as aggregate safe reason codes;
the watchdog warns on failures from the last 30 minutes without storing a user,
prompt, image, payment URL or provider secret.
Expired unpaid orders are reported as an informational aggregate and do not
degrade watchdog health unless a paid/grant/receipt/duplicate invariant fails.

The provider check is configuration-only: it verifies the selected provider,
model and credential presence without calling Gemini or generating an image.
Operational reports and payment reconciliation open the existing SQLite file in
`mode=ro` with `query_only=ON`; they do not initialize or migrate the database.

## Backup and recovery proof

The scheduled backup creates the legacy encrypted SQLite artifact and a separate
encrypted recovery bundle containing SQLite plus `data/users`. The GitHub runner
copies and stores only encrypted artifacts and verifies their SHA-256 values; it
never decrypts production data or runs cleanup. Cleanup is a separate,
explicitly authorized maintenance operation.

Every recovery bundle requires a manifest with its version, timestamp, deployed
source revision, included components and encrypted status. Code, site and
non-secret host configuration are restored from the recorded Git revision;
`.env`, TLS/SSH material and provider/payment credentials must come from approved
secret storage and are never included in a backup.

Real-data restore proof may run only as a separately authorized operation on a
trusted host. It must never run on a GitHub-hosted runner. Create and test with
the passphrase on stdin; a retained restore root must be outside `/opt/photo-bot`
and empty:

```bash
printf '%s\n' "$BACKUP_PASSPHRASE" | venv/bin/python -m app.main recovery-create --passphrase-stdin
printf '%s\n' "$BACKUP_PASSPHRASE" | venv/bin/python -m app.main recovery-restore-test --backup <ravuna-recovery-file> --restore-root /var/tmp/ravuna-recovery-proof --passphrase-stdin
```

The report proves archive/hash integrity, SQLite `quick_check`, migration level,
required components and overall `PASS/FAIL`. It never starts the service or
copies restored state over production.

Automated CI restore drills use only synthetic SQLite and synthetic files via
`tests.test_backup_maintenance_operations`.
The production backup workflow runs the real-data recovery proof only through
SSH on the trusted production host, validates the manifest, encrypted artifact,
source revision, expected components and SQLite migration, then removes the
isolated `/var/tmp/ravuna-recovery-proof.*` restore root. The GitHub runner sees
and stores encrypted artifacts only.

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
2. Before restart/redeploy/rollback, check external MAX, Gemini, Robokassa and
   DNS/network status from the production host and an independent endpoint.
3. Read the compact status/watchdog and only the relevant privacy-safe log tail.
4. Prove whether the cause is external or internal; do not restart for an
   unconfirmed external outage.
5. For a proven internal cause, use the smallest reversible containment action.
6. Preserve payment/entitlement evidence and restore exact pre-test state after
   any diagnostic mutation.

## Payment support

Use masked operator commands and dry-run first:

```bash
venv/bin/python -m app.main payment-show --invoice <invoice> --format human
venv/bin/python -m app.main payment-reconcile --invoice <invoice> --format human
venv/bin/python -m app.main payment-reconciliation --format human
venv/bin/python -m app.main payment-resend-original --invoice <invoice>
venv/bin/python -m app.main payment-mark-delivery-retry --invoice <invoice>
```

Apply/retry only with explicit authority and an operator ticket/idempotency key.
Never grant manually before reconciling the callback and ledgers.

## Provider incidents

- If Gemini or manually selected OpenAI is slow, keep the user’s job state
  durable and communicate delay.
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
