# Security and privacy

## Data flow

```text
MAX user -> MAX API -> Ravuna VPS -> private SQLite/storage
                              -> OpenAI (photo + technical edit prompt)
Robokassa -> signed ResultURL -> payment/ledger records
Ravuna VPS -> MAX API -> preview/original delivery
```

Ravuna does not receive full bank-card details.

## Stored data

- opaque MAX user/chat relation and dialog state;
- legal consent version/time;
- source photos, generated originals and watermarked previews;
- GalleryItem/GalleryVersion lineage and product metadata;
- generation/delivery operational events;
- payment, receipt, credit and entitlement audit records.

## Retention defaults

- demo/source/result: 30 days;
- paid gallery work: 180 days;
- trash: 30 days;
- temporary files: 24 hours;
- optional provider context: disabled in production unless separately approved.

## Secret handling

- Secrets live only in untracked `.env`, production `.env` and GitHub Environment.
- Production `.env` is mode 600.
- Credentials are passed via stdin where supported, never printed or committed.
- Secret scans cover tracked/untracked non-ignored repository files before release.
- Chat-posted credentials are treated as disclosed and not copied into docs/code.

## Storage and delivery

- Image files are outside public nginx roots.
- Paths are scoped to opaque user/work IDs and validated before access.
- Cleanup does not follow symlinks.
- Original delivery requires ownership plus an available/consumed entitlement.
- Watermarked preview and original are distinct stored artifacts.
- Logs and reports must not contain images, raw prompts or platform identifiers.

## Payment security

- SHA-256 only for the active Robokassa integration.
- ResultURL validates signature, amount, invoice and expected order state.
- Browser redirects never mutate payment or entitlement state.
- Callback/grant/receipt operations are idempotent.
- Refund execution is disabled by default/current baseline until explicitly enabled.

## Production isolation

- Runtime uses `photoapp` and one hardened systemd service.
- `NoNewPrivileges` and scoped writable paths are configured.
- `.env`, data, logs and temp are excluded from code synchronization.
- A fresh installation is fail-closed.
- Ordinary deploy preserves the approved production operating state.

## Privacy-safe operations

Operational output may include counts, booleans, modes, masked references,
durations and aggregate failure rates. It must not include tokens, passwords,
MAX IDs, full invoice identifiers, prompts, image bytes or private absolute paths.

Engineering documentation does not replace legal review. Public legal texts are
maintained in `site/public/legal/` and must remain consistent with actual data flow.
