# Architecture

## Scope

Ravuna is a single Python 3.12 application with a MAX polling transport, SQLite,
private filesystem storage, replaceable image-provider adapters and Robokassa
payments. The static website is deployed independently behind nginx.

## Request flow

```text
MAX update
  -> transport normalization
  -> access/observe guard
  -> dialog state machine
  -> source validation and private storage
  -> exact Unicode prompt plus deterministic modular preservation guard
  -> configured provider router
  -> OpenAI or Google Gemini image edit
  -> original persistence
  -> watermarked preview
  -> GalleryItem/GalleryVersion lineage
  -> MAX delivery and debit commit
```

The application acknowledges no successful edit until provider output, storage,
gallery persistence and delivery reach their required states. Technical or
delivery failures must not consume the user’s edit allowance.

## Module boundaries

- `config.py`: immutable environment-derived settings.
- `database.py`: schema, migrations and transaction helpers.
- `storage.py`: scoped private paths, validation and safe deletion.
- `max_transport.py`: Bot API HTTP and polling only.
- `max_application.py`: product states, messages and callbacks.
- `direct_prompt.py`: keeps the exact Unicode text received from the product flow
  as the first prompt segment and appends only deterministic protections for
  attributes the request does not explicitly change. It performs no translation,
  artistic expansion or LLM analysis.
- `edit_intent.py` and `prompt_builder.py`: preserved legacy prompt layer, selected
  only when direct prompting is disabled.
- `provider_router.py`: fail-fast selection of `openai`, `gemini` or
  `nanobanana`; `nanobanana` is a Gemini API routing alias, not a separate API.
- `image_provider.py`, `gemini_image_provider.py` and `openai_client.py`:
  provider-specific external image execution.
- `demo_service.py`: attempt lifecycle, watermark and delivery handoff.
- `gallery.py`: work/version lineage and user operations.
- `commerce.py`: credit and entitlement ledgers.
- `payments.py`, `robokassa.py`, `payment_webhook.py`: payment boundary.
- `operations.py`: read-only operational aggregation.

## Core data model

- One GalleryItem represents one logical source/work.
- GalleryVersion is immutable and points to its parent/source lineage.
- GenerationAttempt records provider and delivery lifecycle separately.
- Credit ledger records edits; entitlement ledger records original rights.
- PaymentIntent and PaymentOrder separate UX intent from provider confirmation.
- Receipt and callback events form an auditable, idempotent payment trail.

## Concurrency and durability

- SQLite transactions enforce ledger and callback idempotency.
- User-level and global generation limits prevent duplicate concurrent work.
- Files are written privately before their database references become usable.
- One systemd service is the only polling runtime.
- Migrations are forward-only and validated during deployment.

## External boundaries

- MAX: transport, media download and user delivery.
- Google Gemini or OpenAI: photo plus direct edit prompt; no payment or account
  data. The repository default is Gemini Nano Banana Pro with model id
  `gemini-3-pro-image`; OpenAI remains selectable without automatic fallback.
- Robokassa: order/amount/receipt/signature; no photo or prompt.
- GitHub Actions: source deployment and encrypted backup artifacts.
- nginx: static site and ResultURL reverse proxy only.

## Invariants

- Preview is watermarked; original is private.
- Corrections use the selected parent version.
- ResultURL is authoritative for payment state.
- Browser redirects never grant product rights.
- Duplicate callbacks cannot duplicate ledger entries or receipts.
- Failures do not debit edit allowance.
- Production state is preserved across ordinary deploys.
- Fresh installs are fail-closed.

See [UX](UX.md), [Payments](PAYMENTS.md), [Production](PRODUCTION.md) and
[Security](SECURITY.md).
