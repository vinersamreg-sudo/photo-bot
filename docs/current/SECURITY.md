# Security and privacy

## Data flow

```text
MAX user -> MAX API -> Ravuna VPS -> private SQLite/storage
                              -> Gemini (photo + exact user prompt)
                              -> OpenAI only when manually selected
Robokassa -> signed ResultURL -> payment/ledger records
Ravuna VPS -> MAX API -> preview/original delivery
Content Studio -> approved MAX/Telegram/VK destination (machine-gated demo only)
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

The retention timer runs independently of the bot only after its first
backup/dry-run gate has passed. Cleanup is locked and idempotent, never follows
symlinks, refuses paths outside private storage, and reports only aggregate
counts/bytes. All DB-referenced primary and secondary sources, results and
pending-edit inputs are excluded from orphan deletion until their owning gallery
retention is due.

## Secret handling

- Secrets live only in untracked `.env`, production `.env` and GitHub Environment.
- Production `.env` is mode 600.
- Credentials are passed via stdin where supported, never printed or committed.
- Secret scans cover tracked/untracked non-ignored repository files before release.
- Chat-posted credentials are treated as disclosed and not copied into docs/code.
- Content publishing uses isolated per-platform settings with minimum channel or
  community rights. MAX may reuse the official Ravuna bot token only for the
  Ravuna channel; credentials are never stored in Content Studio SQLite.

## Storage and delivery

- Image files are outside public nginx roots.
- Paths are scoped to opaque user/work IDs and validated before access.
- Cleanup does not follow symlinks.
- Original delivery requires ownership plus an available/consumed entitlement.
- Watermarked preview and original are distinct stored artifacts.
- Logs and reports must not contain images, raw prompts or platform identifiers.
- Gemini prompt passthrough must not be weakened by hidden preservation text;
  prompt privacy is enforced by excluding raw prompts from telemetry and reports.

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

Content Studio accepts only rights-verified Ravuna demonstration assets. External
publishing requires a global flag and a second platform-specific flag, and only
manifest-verified, machine-approved scheduled posts are eligible. Publication
errors contain error classes and HTTP status only, never remote bodies, tokens or
media bytes. The autonomous runtime has separate state and writable paths; its
access to the product SQLite is `mode=ro`/`query_only` and limited to aggregate
attribution counts. Customer IDs, prompts, images and payment secrets are neither
selected nor copied into marketing storage or analytics.
The approved source root is `marketing/assets/approved/`; runtime copies stay in
the separate Content Studio storage, and publisher transports reject any media
path outside it. This is a hard path boundary from private customer storage.

Engineering documentation does not replace legal review. Public legal texts are
maintained in `site/public/legal/` and must remain consistent with actual data flow.
