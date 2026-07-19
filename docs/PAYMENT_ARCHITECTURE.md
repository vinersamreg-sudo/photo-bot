# Payment Architecture

## Components

- `PaymentProvider` is the narrow provider boundary; `RobokassaProvider` owns signatures, payment links and refund HTTP.
- `PaymentService` owns all durable state transitions and exact-version authorization.
- `PaymentWebhookServer` is a small loopback-only ResultURL listener, intended to sit behind the existing HTTPS reverse proxy.
- SQLite migration v8 adds orders, attempts, events, webhook checks, receipts, audit and refunds.
- `MaxApplication` creates a payment link or re-delivers an already paid exact original.

## Durable entities

`PaymentIntent` captures purchase intent; `PaymentOrder` binds user, attempt and version to one invoice; `PaymentAttempt` records link/delivery activity; `PaymentEvent` and `PaymentWebhook` provide idempotent callback evidence; `PaymentReceipt` stores fiscal preparation state; `PaymentAudit` is append-only operational history. Refunds have separate intent and audit tables.

## Transaction boundary

Callback processing inserts the event, validates it and changes order/intent/version state in one SQLite transaction. Only `gallery_versions.id = payment_orders.version_id` is unlocked. Delivery happens after commit. MAX failure moves the paid order to `delivery_pending`; it never rolls payment back and never requires another charge.

## Concurrency

SQLite serializes writers. Unique invoice, public token, idempotency key and event digest constraints prevent double order creation and replay. A different valid callback for the same paid invoice is recognized from order state and audited without a second unlock. Delivery attempts are separate and may legitimately be repeated.

## Retention

Paid originals retain the paid retention policy. Payment audit rows intentionally survive gallery cleanup; they store opaque internal IDs and financial state, not images, prompt text, platform user IDs or secrets. Legal/accounting retention duration still requires specialist approval before sales.
