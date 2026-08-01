# Legacy brand reference registry

This migration note records every class of predecessor-brand reference that is intentionally retained after the Ravuna rebrand.

## Historical evidence

The name Pixora remains in:

- `CHANGELOG.md`;
- `DECISIONS.md`;
- dated audit and validation reports;
- the original Robokassa support decision;
- the former v1 product-model report;
- historical signature-bisect and sandbox-E2E evidence.

Changing those texts would falsify the project history and the wording of evidence captured at the time.

## Immutable compatibility identifiers

The following persisted or externally bound identifiers are intentionally unchanged:

- `created_for_pixora` in Content Studio rows and schema checks;
- `pixora_owned` in processing/source metadata;
- `pixora-product-subject` in stable privacy-preserving subject hashes;
- `pixora-mask-*` and `pixora-composite-*` in orphan cleanup;
- `pixora-*.sqlite3.enc` in backup discovery;
- `https://pixoraai.ru/payments/robokassa/result` in the active Robokassa callback contract;
- the old-domain Nginx vhost and `/opt/pixora-site` compatibility mirror required by that callback.

These values are not shown as the current public brand. Renaming them would risk losing access to existing database rows, storage cleanup, backup discovery, telemetry continuity or payment callbacks.

## Public rule

All active user copy, payment descriptions, Receipt items, MAX messages, static pages, legal pages, SEO, metadata, manifests, social cards, operator-facing current documentation and deployment labels use Ravuna.
