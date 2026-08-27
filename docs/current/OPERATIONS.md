# Operations

## Compact status

On production, use the read-only summary first:

```bash
cd /opt/photo-bot
venv/bin/python scripts/production_status.py --root /opt/photo-bot
```

It reports SHA, systemd/runtime/restarts, health, SQLite, processing, orphan state
and non-secret operating flags. It must not mutate `.env`, SQLite or storage.

The report distinguishes `MAIN_BOT_DEPLOYED_SHA` from
`CONTENT_STUDIO_DEPLOYED_SHA`. Canonical ancestry requires a read-only Git
checkout with current `origin/main` objects:

```bash
venv/bin/python scripts/production_status.py \
  --root /opt/photo-bot \
  --content-studio-root /opt/ravuna-content \
  --canonical-repository /path/to/read-only/canonical-checkout
```

It then reports `ORIGIN_MAIN_SHA`, ancestry `PASS/FAIL` for both deployed SHAs
and the informational number of commits by which `main` is ahead. Production is
not required to equal `main` head. A deployed SHA missing from canonical history
is an error; unavailable Git evidence is reported as `unavailable`, never guessed.

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
encrypted recovery bundle containing the application SQLite/private storage plus
the Content Studio SQLite and its rights-cleared media storage. The GitHub runner
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
The production backup workflow runs both the legacy SQLite restore check and the
real-data recovery proof only through SSH on the trusted production host. It
validates `quick_check`, migration, manifest, encrypted artifact, source revision
and expected components, then removes the isolated
`/var/tmp/ravuna-recovery-proof.*` restore root. The GitHub runner sees and stores
encrypted artifacts only.

## Content Studio publishing

Content Studio remains globally fail-closed until
`CONTENT_STUDIO_PUBLISHING_ENABLED=true`. MAX, Telegram and VK each also require
their own publishing flag, dedicated credential and destination ID. A platform
cannot borrow another platform's credential. Tokens are excluded from status,
publication-attempt payloads and errors.

Raw inputs must be present under `marketing/assets/approved/` and listed with a
rights record. Content Studio copies approved inputs into its isolated
`data/content_studio/storage`; both ingestion and platform transports reject paths
outside those roots. Customer `data/users` is never an eligible marketing source.

Every generated CTA uses the official MAX bot deep-link form
`?start=src_<platform>-<content-hash>` (plus UTMs for external analytics). The
existing bot attribution parser accepts this bounded payload, so starts, first
photos, generations and payments can be joined to the Content Studio source code.
Views remain a platform-side metric and are recorded as aggregate snapshots.

Full-auto mode additionally requires `CONTENT_STUDIO_FULL_AUTO_ENABLED=true`.
It maintains at least seven future days, rotates all approved themes with a 20%
exploration floor, and machine-approves only posts whose manifest checksum,
commercial-use flag, before/after difference, media format, CTA and attribution
all pass immediately before publication. A failed check leaves the item
unpublished and records only a safe failure class.

Before every send, the persistent Novelty Gate compares the candidate with at
least the latest 30 published posts. It blocks reused asset checksums, perceptual
image duplicates, normalized exact text, text similarity of 0.82 or greater,
occupied publication slots and avoidable three-post category runs. UUIDs, UTMs,
timestamps, hashtags and standard CTA/disclosure text do not create novelty.
After at most five candidates, an exhausted pool is recorded and skipped; a
duplicate is never published as fallback. Schema version 5 also stores durable
publication history, slot ownership and send claims so concurrent or retried
scheduler runs cannot publish the same item twice.

Queue selection considers only platform flags that are currently enabled. Due
items are read through their durable slot owner and selected fairly per enabled
platform; a disabled-platform backlog therefore cannot consume the bounded batch
or starve MAX. Queue maintenance likewise creates work only for enabled
platforms. To normalize legacy queues after flags or scheduler behavior change,
preview and then apply the idempotent reconciliation command:

```bash
venv/bin/python -m app.content_studio.cli content reconcile-queue
venv/bin/python -m app.content_studio.cli content reconcile-queue --apply
```

Reconciliation archives scheduled work for disabled platforms, skips overdue
catch-up slots, collapses competing rows to one unused asset per future slot and
then refills from the current day. It never deletes published rows or publication
history, so an externally deleted MAX post remains consumed by the Novelty Gate.

Operator preview and one-cycle execution:

```bash
venv/bin/python -m app.content_studio.cli content auto-run
venv/bin/python -m app.content_studio.cli content auto-run --apply
venv/bin/python -m app.content_studio.cli content dashboard --days 7
```

The `ravuna-content-publisher.timer` runs the same bounded full-auto cycle every
15 minutes from `/opt/ravuna-content`, with its own venv, env, SQLite and storage.
It has read-only access to `/opt/photo-bot/data` for aggregate attribution only;
it cannot restart or write the product service. Each destination remains
independently fail-closed until its permission audit and platform flag pass.
Failed posts remain auditable and are not silently marked published.

MAX publishes one image post daily at 19:00 Samara. VK schedules one vertical
H.264 1080x1920 video daily at 20:00 and a wall before/after post on Monday,
Wednesday and Friday at 18:30. Polling every 15 minutes does not increase these
limits. The official VK wall/video methods require a user token, so
`CONTENT_STUDIO_VK_TOKEN_TYPE` must remain `user`; unsupported token types fail
closed. Video generation uses only the separate local FFmpeg runtime and never
invokes the production image provider or an AI video API.

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
