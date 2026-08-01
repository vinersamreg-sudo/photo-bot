# Ravuna soft launch — 29 July 2026

## Launch baseline

- Launch window opened: `2026-07-29T12:34:02+04:00`.
- Branch: `main`.
- Confirmed application SHA before launch: `40bf8fce2677c5e2870e9936a303fa12200c6317`.
- Working tree: two unrelated local Robokassa bisect edits exist and are excluded from this launch.
- Database migration: `9`.
- Database size before launch: `966656` bytes.
- Production database `PRAGMA quick_check`: `ok`.
- Processing attempts: `0`.
- Active dialogs: `0`.
- Orphan files: `0`.
- Healthcheck: `OK`.
- systemd: `active`, `enabled`.
- Runtime processes: `1`.
- systemd restart count: `0`.
- Ravuna HTTPS and required legal pages: `HTTP 200`; `www` redirects to the canonical host.
- OpenAI model/key: `gpt-image-2`, configured. Dashboard-confirmed balance at the launch gate: `$19.99`.
- Robokassa production credential names are configured; values are never recorded here.
- Historical monitoring note: one rejected ResultURL event from `2026-07-26` has reason `unknown_invoice`. It predates this launch and is not a current callback failure.

## Backup

- SQLite online-backup:
  `/opt/photo-bot/data/backups/ravuna-soft-launch-20260729T083244380339Z.sqlite3`
- Size: `966656` bytes.
- Created: `2026-07-29T08:32:44.385717+00:00`.
- SHA-256:
  `c1fd03429f2976504741c5f1493b54e18be94461caa7250bc1e348ffa0b23e4b`.
- Backup `PRAGMA quick_check`: `ok`.
- Redacted flag snapshot:
  `/opt/photo-bot/data/backups/ravuna-soft-launch-20260729T083244380339Z.flags.json`.

The backup was made through SQLite's online backup API while the application
was running. Existing backups were not removed.

## Feature flags

| Flag | Before launch | Soft-launch target |
| --- | --- | --- |
| `MAX_POLL_OBSERVE_ONLY` | `true` | `false` |
| `MAX_TRANSPORT_MODE` | `polling` | `polling` |
| `PILOT_USER_LIMIT` | `0` | `0` |
| `OPENAI_IMAGE_REQUESTS_ENABLED` | `true` | `true` |
| `OPENAI_IMAGE_MODEL` | `gpt-image-2` | `gpt-image-2` |
| `PAYMENTS_ENABLED` | `false` | `true` |
| `PAYMENT_PROVIDER` | `disabled` | `robokassa` |
| `PAYMENT_WEBHOOK_LISTENER_ENABLED` | `true` | `true` |
| `PAYMENT_WEBHOOK_ENABLED` | `false` | `true` |
| `PAYMENT_REFUNDS_ENABLED` | `false` | `false` |
| `ROBOKASSA_MODE` | `sandbox` | `production` |
| `ROBOKASSA_PRODUCTION_APPROVED` | `false` | `true` |

This is an owner-only production soft launch. `PILOT_USER_LIMIT=0` and the
existing owner allowlist stay unchanged. It is not a public rollout.

## Previously accepted UX evidence

- `/start` and the combined photo-plus-caption entry.
- Preview delivery with watermark.
- Correction and another-variant actions.
- Gallery, version history, main menu and new-photo transitions.
- Original unlock, delivery and repeat delivery without a second payment.
- Stale inline keyboards are deactivated after state transitions.
- Disabled intake gives an explicit temporary-unavailability response.

No paid OpenAI image request and no payment is part of the launch procedure.

## Exact emergency rollback

Run from the repository workstation:

```powershell
$rollback = @'
set -euo pipefail
ROOT=/opt/photo-bot
set_env() {
  NAME="$1"
  VALUE="$2"
  TMP="$ROOT/.env.rollback.tmp"
  grep -v "^$NAME=" "$ROOT/.env" > "$TMP" || true
  printf '%s=%s\n' "$NAME" "$VALUE" >> "$TMP"
  chown photoapp:photoapp "$TMP"
  chmod 600 "$TMP"
  mv "$TMP" "$ROOT/.env"
}
set_env MAX_POLL_OBSERVE_ONLY true
set_env PILOT_USER_LIMIT 0
set_env PAYMENTS_ENABLED false
set_env PAYMENT_PROVIDER disabled
set_env PAYMENT_WEBHOOK_LISTENER_ENABLED true
set_env PAYMENT_WEBHOOK_ENABLED false
set_env PAYMENT_REFUNDS_ENABLED false
set_env ROBOKASSA_MODE sandbox
set_env ROBOKASSA_PRODUCTION_APPROVED false
set_env ROBOKASSA_SANDBOX_DUPLICATE_PROBE false
set_env ROBOKASSA_SANDBOX_ORDER_BASELINE 0
set_env MAX_TRANSPORT_MODE polling
systemctl restart photo-bot.service
systemctl is-active photo-bot.service
cd "$ROOT"
sudo -u photoapp venv/bin/python -m app.main health
test "$(pgrep -fc '/opt/photo-bot/venv/bin/python -m app.main run')" = 1
'@
$rollback | ssh root@116.203.24.102 bash -s
```

The normal deployment rollback is:

```powershell
gh workflow run deploy.yml `
  --repo vinersamreg-sudo/photo-bot `
  --ref main `
  -f enable_owner_handlers=false `
  -f reset_owner_dialog=false `
  -f grant_owner_e2e_attempts=0 `
  -f pilot_user_limit=0
```

That workflow re-applies the repository's fail-closed production defaults,
deploys the same application tree and restarts one systemd runtime.

## Launch checklist

- [x] Expected branch and pre-launch SHA confirmed.
- [x] Unrelated local changes identified and excluded.
- [x] systemd active; one runtime; restart count zero.
- [x] Healthcheck and SQLite quick-check pass.
- [x] Processing and orphan counts are zero.
- [x] HTTPS, legal pages and ResultURL route are reachable.
- [x] Required production credential names are configured.
- [x] OpenAI balance is above the configured warning threshold.
- [x] Online backup and redacted flag snapshot are valid.
- [x] Exact rollback commands prepared before mutation.
- [ ] Launch documentation commit deployed successfully.
- [ ] OpenAI manual balance confirmation recorded in runtime configuration.
- [ ] Owner-only handlers enabled.
- [ ] Production Robokassa payment and ResultURL processing enabled.
- [ ] Post-restart health, database, runtime and log checks pass.
- [ ] Safe MAX walkthrough completes without generation or payment.

## Post-launch checklist

- [ ] Production SHA matches the successful GitHub deployment.
- [ ] `photo-bot.service` is active and enabled.
- [ ] Exactly one application runtime and one loopback webhook listener exist.
- [ ] Restart count remains zero after the intentional restart baseline.
- [ ] Healthcheck and SQLite quick-check pass.
- [ ] Processing and orphan counts are zero.
- [ ] ResultURL returns `405` to GET and no longer returns the disabled `503`
      gate to an oversized POST probe that cannot create a payment event.
- [ ] `robokassa-health` reports production runtime ready.
- [ ] No new `PaymentIntent`, order, receipt or charge was created by launch.
- [ ] No OpenAI image request was made by launch.
- [ ] MAX `/start`, navigation, gallery/history and new-photo entry react
      visibly for the owner without reaching generation.
- [ ] Monitoring summary captured for the launch baseline.

## Operator checks

After one hour:

- Run `python -m app.main monitoring-status --format human`.
- Run `python -m app.main health-report --online --format human`.
- Confirm one runtime, zero stuck processing, zero orphans and no new rejected
  callbacks.
- Review MAX delivery/provider errors and actual OpenAI balance.

After 24 hours:

- Repeat the one-hour checks.
- Reconcile paid orders, receipts, grants and original delivery.
- Compare actual OpenAI usage and Robokassa commission with the cost report.
- Confirm a fresh backup, restore test and off-site copy.
- Stop intake with the rollback above on any payment/data loss, original
  leakage, uncontrolled spend, repeated stuck processing or delivery failure.
