# Ravuna repository context

Read [AGENTS.md](AGENTS.md) first. This file is a compact technical map, not a
history journal. Current operational details live in `docs/current/`; superseded
reports live in `docs/archive/`.

## System

Ravuna is a MAX-based AI photo editor. A user sends a photo and instruction,
receives a watermarked preview, can correct/repeat the work, and can buy the
49 ₽ access package containing two edits and one unwatermarked original.

```text
MAX user
  -> MAX Bot API polling
  -> app/max_application.py
  -> SQLite + private filesystem storage
  -> OpenAI image edit
  -> original + server-side watermarked preview
  -> MAX delivery

Robokassa
  -> POST ResultURL
  -> signature verification + idempotent order transition
  -> credit ledger (+2) + entitlement ledger (+1) + one sale receipt
```

## Main modules

- Runtime/CLI: `app/main.py`.
- Settings: `app/config.py`.
- MAX: `app/max_application.py`, `app/max_transport.py`.
- Editing: `app/edit_intent.py`, `app/prompt_builder.py`,
  `app/openai_client.py`, `app/image_provider.py`.
- State: `app/database.py`, `app/storage.py`, `app/gallery.py`.
- Commercial: `app/commerce.py`, `app/payments.py`, `app/robokassa.py`,
  `app/payment_webhook.py`.
- Operations: `app/operations.py`, `scripts/`, `.github/workflows/`.
- Website: `site/public/`, deployed separately from the bot.

## Core entities

- User/dialog/session: MAX identity mapping and current interaction state.
- GalleryItem: one logical photo work.
- GalleryVersion: immutable lineage node with source/parent/result metadata.
- GenerationAttempt: provider and delivery lifecycle.
- CreditAccount/CreditLedger: edit balance and auditable adjustments.
- OriginalEntitlement/EntitlementLedger: right to one unwatermarked original.
- PaymentIntent/PaymentOrder: purchase lifecycle and idempotency boundary.
- Receipt/PaymentEvent: fiscal and callback audit.

## Production topology

- `/opt/photo-bot`, user `photoapp`, Python 3.12.
- One `photo-bot.service` systemd runtime.
- `/opt/photo-bot/.env` (mode 600), `data/`, `logs/`, `temp/`.
- SQLite plus private filesystem storage.
- `https://ravuna.ru` static site under `/opt/ravuna-site/current`.
- Hetzner VPS, nginx HTTPS, GitHub Actions deployment.

## Non-negotiable invariants

- Active public brand is Ravuna; internal legacy identifiers may remain.
- Ordinary deploy preserves the current working production flags.
- A fresh server is fail-closed.
- Current approved production is public, handlers active, image requests enabled,
  and Robokassa production payments/webhook enabled; refunds remain disabled.
- Never grant from browser SuccessURL or FailURL; ResultURL is authoritative.
- Payment callbacks, credits, entitlements and receipts remain idempotent.
- No original before entitlement consumption; preview is watermarked.
- Failed provider/delivery work does not consume edit allowance.
- Tests restore the exact pre-test runtime state, including public access.
- Never expose secrets, platform IDs, prompts, images or private paths in output.
- Never shut down the laptop without a direct command in the current task.

## Current documentation

- [Architecture](docs/current/ARCHITECTURE.md)
- [UX](docs/current/UX.md)
- [Payments](docs/current/PAYMENTS.md)
- [Production](docs/current/PRODUCTION.md)
- [Deploy](docs/current/DEPLOY.md)
- [Operations](docs/current/OPERATIONS.md)
- [Backlog](docs/current/BACKLOG.md)
- [Sprint template](docs/current/SPRINT_TEMPLATE.md)
- [Economics](docs/current/ECONOMICS.md)
- [Security](docs/current/SECURITY.md)

Use `python scripts/test_fast.py <profile>` during scoped work and
`python scripts/test_release.py` once for release readiness.
