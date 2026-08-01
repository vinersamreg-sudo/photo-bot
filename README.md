# Ravuna

Ravuna is a commercial AI photo editor for MAX. Users submit a photo and a short
instruction, receive a watermarked preview, iterate through corrections/variants,
and can buy a package with two edits and one unwatermarked original.

## Start here

Agents must read [AGENTS.md](AGENTS.md), then
[AI_REPOSITORY_CONTEXT.md](AI_REPOSITORY_CONTEXT.md). Current documentation is in
[`docs/current`](docs/current); historical reports are in
[`docs/archive`](docs/archive).

## Local setup

Python 3.12 is required.

```powershell
cd C:\Users\viner\Documents\Codex\photo-bot
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python scripts/test_fast.py max
```

`.env.example` is intentionally fail-closed. Never commit `.env` or credentials.

## Efficient verification

```powershell
python scripts/test_fast.py max
python scripts/test_fast.py payments
python scripts/test_fast.py openai
python scripts/test_fast.py site
python scripts/test_fast.py storage
python scripts/test_release.py
```

Fast profiles route to existing `unittest` modules and print a short summary.
Release verification runs the complete backend/site suites, secret scan, compile
and diff checks once.

## Main operator commands

```bash
python -m app.main health
python -m app.main launch-status
python -m app.main payment-status --format human
python -m app.main storage-status --format human
python -m app.main maintenance-cleanup
python scripts/production_status.py --root /opt/photo-bot
```

Cleanup is dry-run by default. Real payments, refunds, external image requests and
production mutations require explicit authorization.

## Documentation

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

The production service is public and commercial. Ordinary deploys preserve its
approved operating state; fresh installations remain fail-closed.
