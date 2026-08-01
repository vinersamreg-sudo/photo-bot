# Ravuna public opening — 29 July 2026

## Pre-opening baseline

- Snapshot time: `2026-07-29T13:32:24+04:00`.
- Expected and deployed SHA:
  `c285bd76d8964151e71487b0d00b27b7721bf5fd`.
- Branch: `main`.
- Working tree: two unrelated local Robokassa bisect edits exist and are
  explicitly excluded from this launch.
- Access: owner allowlist enforced; the owner record and allowlist are present.
- `MAX_POLL_OBSERVE_ONLY=false`.
- `MAX_TRANSPORT_MODE=polling`.
- `PILOT_USER_LIMIT=0`.
- `OPENAI_IMAGE_REQUESTS_ENABLED=true`; model `gpt-image-2`.
- `PAYMENTS_ENABLED=true`; provider `robokassa`.
- Payment webhook listener and callback processing: enabled.
- Robokassa mode: production; SHA-256; production approval enabled.
- Refund execution: disabled.
- systemd: active and enabled.
- Runtime processes: `1`; loopback webhook listeners: `1`.
- systemd restart count: `0`.
- Healthcheck: `OK`; SQLite `PRAGMA quick_check`: `ok`.
- Active processing attempts: `0`; stale processing: `0`.
- Orphan files: `0`.
- Payment intents: `2 paid`, `3 expired`; no pending intent.
- Payment orders: `1 delivered`, `1 paid`, `3 expired`; no pending order.
- Original delivery pending: `0`.
- Ravuna home: `HTTP 200`.
- Configured ResultURL on the existing canonical payment host:
  `GET 405`; an unsigned `POST` is rejected with `409`.
- Recent startup/runtime errors: none.
- Historical monitoring exception: one rejected callback with the known
  `unknown_invoice` history remains unchanged and is not part of this launch.

## Backup

No duplicate full backup was created. The already verified launch backup is:

`/opt/photo-bot/data/backups/ravuna-soft-launch-20260729T083244380339Z.sqlite3`

Its recorded SHA-256 is
`c1fd03429f2976504741c5f1493b54e18be94461caa7250bc1e348ffa0b23e4b`;
the backup passed SQLite quick-check. The matching redacted flag snapshot is
stored beside it. The owner identifier and all credentials are intentionally
absent from this document.

## Public-access change

`MAX_PUBLIC_ACCESS_ENABLED` is a fail-closed boolean. Its default is `false`.
When `true`, the MAX application accepts users outside the retained owner/pilot
allowlists. The allowlists and existing owner data are not removed, so access
can be rolled back without touching user, gallery, credit or payment data.

No tariff, free limit, UX text, OpenAI provider, payment, webhook, receipt or
refund behavior is changed by the public-access switch.

## Exact owner-only rollback

This rollback changes only the public-access gate. It intentionally preserves
the current payment, webhook, provider and observe-only settings.

```powershell
$rollback = @'
set -euo pipefail
ROOT=/opt/photo-bot
set_env() {
  NAME="$1"
  VALUE="$2"
  TMP="$ROOT/.env.public-rollback.tmp"
  grep -v "^$NAME=" "$ROOT/.env" > "$TMP" || true
  printf '%s=%s\n' "$NAME" "$VALUE" >> "$TMP"
  chown photoapp:photoapp "$TMP"
  chmod 600 "$TMP"
  mv "$TMP" "$ROOT/.env"
}
set_env MAX_PUBLIC_ACCESS_ENABLED false
systemctl restart photo-bot.service
sleep 3
test "$(systemctl is-active photo-bot.service)" = active
test "$(systemctl is-enabled photo-bot.service)" = enabled
test "$(systemctl show photo-bot.service -p NRestarts --value)" = 0
test "$(pgrep -u photoapp -f '^/opt/photo-bot/venv/bin/python -m app.main run$' | wc -l)" = 1
cd "$ROOT"
sudo -u photoapp env PYTHONPATH="$ROOT" venv/bin/python -m app.main health
grep -qx 'MAX_PUBLIC_ACCESS_ENABLED=false' "$ROOT/.env"
grep -qx 'MAX_POLL_OBSERVE_ONLY=false' "$ROOT/.env"
grep -qx 'PAYMENTS_ENABLED=true' "$ROOT/.env"
grep -qx 'PAYMENT_PROVIDER=robokassa' "$ROOT/.env"
grep -qx 'PAYMENT_WEBHOOK_ENABLED=true' "$ROOT/.env"
grep -qx 'ROBOKASSA_MODE=production' "$ROOT/.env"
'@
$rollback | ssh root@116.203.24.102 "tr -d '\r' | bash -s"
```

After rollback, the owner allowlist remains configured and becomes the active
access gate immediately.

## Post-opening evidence

To be filled from production verification after the single launch deploy:

- Deployed SHA: pending.
- Public access: pending.
- systemd/runtime/restart count: pending.
- Health/SQLite/processing/orphan: pending.
- ResultURL behavior: pending.
- External non-allowlisted MAX `/start`: pending.
- OpenAI image requests made by launch: expected `0`.
- Payment intents or charges made by launch: expected `0`.
