# Delivery Process

## Backend deploy

Push to `main` runs Python 3.12 dependency checks, secret/license scans, all unit tests and health. GitHub Environment `production` supplies Hetzner, OpenAI and MAX secrets. rsync is scoped to `/opt/photo-bot` and preserves `.env`, `venv`, `data`, `logs`, `temp` and the separate `site` deployment.

Production env is converged to `OPENAI_IMAGE_MODEL=gpt-image-2`, quality medium, router/composite/segmentation disabled, polling enabled only with a MAX token. Push deploy sets `PILOT_USER_LIMIT=0` and `MAX_POLL_OBSERVE_ONLY=true`. A manual workflow may enable owner handlers and choose 0/5/10/20 pilot users; a positive limit fails before deployment when the secret list is shorter than the requested stage or contains the owner.

Before stopping systemd, deployment checks the live database and aborts if a generation attempt or dialog is actively processing. Server verification then applies migration v6, runs tests, OpenAI/MAX auth checks, restarts one hardened systemd unit, validates MainPID/command/lock, checks restart and duplicate-instance protection, runs SQLite quick_check and `launch-status`, then records the deployed SHA.

## Backup workflow

`Encrypted production backup` runs daily and manually. Required secret: `BACKUP_ENCRYPTION_PASSPHRASE` (plus Hetzner secrets). It creates an online SQLite snapshot, encrypts it, performs a server restore test, copies only the encrypted file off VPS, independently restores it on the runner, uploads a 14-day artifact, marks the off-site copy, executes maintenance cleanup and requires strict launch readiness.

The passphrase is read through stdin and is never printed or stored in metadata. A green workflow is the evidence for backup readiness; code or an encrypted file alone is not.

## Rollback

Rollback is a new reviewed commit and normal deploy; runtime data is preserved. Before a destructive/schema-risk change, run and restore-test a backup. Never edit production code manually.

## Release checklist

1. Clean diff and no unrelated project changes.
2. Secret scan, tests, `pip check`, site tests.
3. Push and green deploy run.
4. Matching Git commit, Actions SHA and production marker.
5. Green backup workflow/restore/off-site/cleanup.
6. `launch-status --strict` for pilot activation.
7. Owner E2E only after agreeing the real OpenAI request maximum.
8. Return observe-only unless continued access was explicitly authorized.
