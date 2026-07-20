# Payment Architecture

## Components

- `PaymentProvider` / `RobokassaProvider`: signed link, ResultURL validation and provider refund boundary.
- `PaymentService`: durable payment intent/order/event/receipt/audit transitions and atomic package grant.
- `CommerceService`: initial grant, credit lots/reservations/ledger, package grants, unlock entitlements, rollback and admin audit.
- `PaymentWebhookServer`: loopback-only POST ResultURL listener behind trusted HTTPS.
- `MaxApplication`: package purchase UX and later user-selected original unlock.
- `payment_admin` and commerce CLI: masked reconciliation and dry-run-first operations.

## Durable entities

Migration v8 retains PaymentIntent/Order/Attempt/Event/Webhook/Receipt/Audit and RefundIntent/Audit. Migration v9 adds:

- `user_credit_accounts`: aggregate available/reserved/granted/consumed/refunded/adjusted totals and a lifetime `free_grant_applied` bit;
- `generation_credit_lots`: initial, paid and admin sources, so refund affects only its package;
- `generation_credit_reservations`: one attempt/idempotency key per reserved credit;
- `credit_ledger`: append-only balance changes;
- `continuation_pack_grants`: atomic +2/+1 grant and usage/refund status;
- `unlock_entitlements`: available/reserved/consumed/cancelled/refunded and optional selected version;
- `commerce_admin_audit`: hashed subject, reason, delta and idempotency.

## Generation transaction boundary

Before provider, one active lot is atomically moved available → reserved. A second concurrent request cannot use it. After successful MAX preview delivery, the reservation and lot become consumed and GalleryVersion remains durable. Every failure path releases the same reservation. Startup recovery releases only reservations whose attempts are no longer pending/processing; it cannot mint new value.

## Payment transaction boundary

Callback processing validates invoice, amount, signature, merchant binding, token and local state. In one SQLite transaction it records the callback, confirms the payment and creates exactly one package grant, a two-credit lot and one entitlement. A repeated callback returns idempotent success. Browser redirect does not call this boundary.

The package is not version-scoped. Later, original delivery checks user ownership,
non-deleted state and private-original presence, reserves the oldest available
entitlement, and sends the file through MAX. Only a successful MAX delivery commits
the reservation and marks that exact version unlocked. A failed delivery releases
the reservation, so the right remains available and no payment is required to retry.
Startup recovery releases any delivery reservation left by an interrupted runtime;
refund holds are distinct because they have no selected GalleryVersion and remain held.

## Refund and concurrency

SQLite serializes writers. Unique invoice, event digest, package payment ID, reservation key, ledger key and entitlement source constraints prevent replay. An unused package rollback removes only its available lot and entitlement. Used/reserved value is manual review. A refund hold makes the specific package non-spendable while retaining audit evidence.

## Retention and privacy

Paid originals use paid retention. Financial/ledger audit survives gallery cleanup but stores opaque internal IDs, hashed subject and bounded enums—not images, prompt text, platform IDs, credentials or private paths. Statutory retention and fiscal wording still require specialist confirmation.
