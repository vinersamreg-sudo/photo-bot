# Architecture

## Runtime

One Python 3.12 process under `photo-bot.service` runs MAX long polling. SQLite is the durable store; private files live under `data/users`; `temp` contains only disposable intermediates. A file lock prevents a second polling consumer. Restart recovery runs only in that primary process after it owns the lock; health, backup and reporting database clients are read-only with respect to runtime state. Recovery marks unfinished generation attempts technical/refunded and best-effort closes any stale MAX status message without blocking startup.

An event already linked to a processing attempt is completed rather than replayed, so a crash cannot silently duplicate a paid provider request. An event interrupted before processing remains retryable. Deployment refuses to stop the service while an active generation or processing dialog exists.

`MAX_POLL_OBSERVE_ONLY=true` builds no handlers or image service. When handlers are enabled, access is checked before dialog creation: owner plus the active 0/5/10/20 prefix of `MAX_PILOT_USER_IDS`. Other users receive the closed-test response without files or OpenAI calls.

## User request path

`MAX event → deduplication → dialog state → validated private source → deterministic EditPlan → English prompt → OpenAI gpt-image-2 images.edit → private original → watermark preview → MAX delivery → quota commit → GalleryVersion`.

Delivery precedes success/quota commit. Timeout/network/quota/policy/storage/delivery errors use separate domain types. Technical failures set `technical_refund=1`. The MAX processing status is edited to success or a precise safe error; no HTTP status, request ID or provider name is shown.

Correction reads the selected parent version's private original and creates a child. Repeat reuses the same effective plan and parent branch. The immutable initial source remains attached to one demo session. Experimental processing router/composite/segmentation code is dormant in v1 (`PROCESSING_MODE_ROUTER_ENABLED=false`); OpenAI `gpt-image-2` is the only production image provider.

## Data

Migration v6 adds `product_events`. It stores event category, internal session/attempt/gallery references, error type, duration, estimate and parser fallback. It deliberately has no prompt, image, platform ID or biometric columns.

Gallery retention covers demo, paid and trash. `maintenance-cleanup` first reports due gallery items, stale temp and unreferenced private files; `--execute` deletes only scoped, non-symlink candidates after their grace period.

## Backup and operations

SQLite online backup API creates a consistent snapshot. OpenSSL encrypts it using AES-256-CBC + PBKDF2/200k iterations. The backup is decrypted into a temporary DB and passes `PRAGMA quick_check`; the encrypted file is copied to a GitHub Actions artifact with 14-day retention. Cleanup follows only after off-site confirmation.

`launch-status` checks systemd, fresh polling, MAX connectivity, OpenAI auth/model, SQLite/migration, disk, backup age/restore/off-site, cleanup/orphans, active processing and daily duration/errors/delivery/cost. Output contains counts and booleans, not user IDs, secrets, prompts or paths.

## Deferred topology

Webhook/queue/workers/object storage and horizontal scale are deferred. They become relevant only after pilot metrics show that one polling process + SQLite is insufficient. The static site is a separate artifact under `site/` and is not published automatically with backend deploy.

## Optional OpenAI Responses context

Migration v7 adds provider-context metadata and privacy-safe events.
`ContextAwareImageProvider` exposes stateless Images API and optional Responses
image-tool modes behind three disabled flags. `ProviderContextService` owns
branch selection, depth/idle reset, fallback and remote deletion. Parent response
comes from the selected version, never from a global latest pointer. Production
deploy forcibly disables the experiment.
