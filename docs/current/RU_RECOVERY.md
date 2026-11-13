# RU Recovery Safety — staging only

This candidate is independent of the infrastructure migration, schema 17 and
the deployed Hetzner workflow. Do not install or enable these timers on the
current VPS. Existing recovery objects and the GitHub backup workflow are
unchanged. A RU VPS, independent RU bucket and read-only restore runner require
separate authorization and a synthetic pilot.

## Publication, capacity and notifications

- Install only the recovery units/config in `ops/ru/` on the authorized
  candidate. Keep credentials outside Git, TLS on, bucket private, and provision
  separate full writer, checkpoint writer and independent reader identities.
  Readers must not write, rotate or delete. Store encryption passphrases
  separately from the bucket and rehearse access from a replacement host.
  Pre-create the shared `data/result-retention.lock` as a regular
  `photoapp`-owned 0600 file. The full writer sandbox only grants write access
  to that file and its own scratch, not the live database or media directory.
- Full snapshot: 03:00 Samara, 23:00 UTC on the previous day. At 06:05 Samara an
  independent runner downloads the scheduled ciphertext and restores every file,
  checks SHA256, media links, FK and commerce. A manual seed cannot satisfy the
  schedule or rotate an established catalogue. Seed two **distinct** states.
- Keep four daily labels and one point from each of the previous two completed
  weeks when available. Matching SQLite/media digests share a point. Keep two
  distinct verified fallbacks when available. At most six live points plus one
  candidate; a failed retirement blocks another candidate until reconciled.
  Checkpoints optionally use a separate index: two verified and one candidate,
  at :00/:15/:30/:45 plus 30 seconds. The checkpoint freshness monitor checks
  index metadata only; its writer performs a full SHA256/SQLite readback each run.
- A distributed reservation is a CAS update to the control index. Only the winner
  can upload, to a random create-only object key. The verified list changes only
  after full readback and restore, by a second CAS. Rotation records retired
  keys durably before deletion; they cannot be reused. A lost/ambiguous commit or
  unconfirmed upload stops automatic writes rather than guessing. A crash leaves
  at most one fenced candidate. After disabling **all** writer identities/units
  and draining in-flight requests, read the current private index and use
  `recover-writer --writers-fenced --interrupted-key <pending-key>` on the
  matching namespace; the command aborts exact-key multipart sessions and CASes
  pending into retirement before deletion. If already published, do not delete.
- Each dedicated scratch path admits only a single owned active work directory
  per purpose. The same host lock must be held before reaping a crashed run.
  Full preflight: 16 GiB free; 8 GiB ciphertext cap, 6 GiB cap for the expanded
  tree, 64 MiB multipart scratch; checkpoint ciphertext 128 MiB. Reserve
  additional space for retained *manual* restores, or use an isolated volume.
  If budget is exhausted, preserve verified copies, alert and stop.
- Acknowledged HTTP 2xx is the success criterion for notification delivery.
  systemd dispatches separate success/failure units with bounded retry.
  On an independent receiver, alert for missing full or checkpoint heartbeats
  as well as failure, so a dead VPS or notification transport cannot be silent.
  Test both positive delivery and missed/failed notifications before GO.

## S3 gate (no real data in the pilot)

Candidate: [Yandex Object Storage Standard](https://yandex.cloud/ru/docs/storage/pricing).
Yandex documents [conditional PUT and multipart completion](https://yandex.cloud/ru/docs/storage/concepts/object),
including create-only `If-None-Match: *`, CAS `If-Match: <ETag>`, and
409/412 conflicts. Enforce those headers using the documented
[bucket policy](https://yandex.cloud/ru/docs/storage/concepts/policy); writer
credentials must not be able to remove that policy. Require an enabled
`AbortIncompleteMultipartUpload` lifecycle (1–2 days) for both object prefixes.
Do not use lifecycle expiration or transition on completed archives, and do
not enable bucket versioning on this rotating namespace. Two verified points
must additionally be protected from writer-credential compromise in a
separately controlled recovery copy; do not turn on Object Lock in the rotating
bucket (it needs versioning). Confirm the storage and restore runner are in РФ.

From a fresh synthetic bucket, prove 412 on a repeated create-only PUT and
multipart completion, reject a stale catalogue ETag and verify checksum and
restore across two actual writers. Simulate a dropped response, incomplete
multipart, rotation failure, missing notification and a corrupt restore.
Then verify a separately credentialed runner can fully restore each of the
two protected points. Official API documentation and local contract tests are
not proof of this account, policy, route or actual alert delivery.

Budget using the observed 3.62 GiB ciphertext proxy and 44.57 MiB SQLite:
21.70 GiB for six full points, 25.32 GiB including candidate, ~326 GiB/month
for three full reads/day and ~125 GiB/month for 96 checkpoint readbacks/day.
At the referenced Standard rates, approximately 437 ₽/month for full backups,
or 648 ₽ with checkpoints, excluding VPS, independent restore runner, separately
protected copies and exceptional downloads. Dedup, actual encryption sizes,
restore retries and pricing change the invoice.

## Restore and financial cutover gate

1. Keep both runtimes and payments fenced. Download a catalogue point from a
   private reader and match its ciphertext size/SHA256; restore into a new
   private directory. Never restore to the application tree.
2. A `ru_db_recovery restore` output explicitly says SQLite-only. It does
   not replace the database in the full restored tree. Run
   `python -m app.ru_recovery_gate --help` and provide the independently
   verified full-state identity, optional checkpoint SHA/start time and
   actual incident time. The gate hashes the materialized media, checks all
   stored originals, previews, correction/source/second-image and pending
   references, and rejects a newer SQLite if required media is missing,
   unproven or if it would expose stale media retired in the newer state.
   Recover the matching newer media with independent evidence or use the
   coherent daily point and reconcile. Do not auto-publish even on PASS.
3. Obtain a complete authenticated merchant export from at/before the full
   snapshot through incident/cutover with an overlap and late confirmations
   or refunds for older invoices. Normalize it privately to
   `{coverage_start,coverage_end,operations:[{invoice,amount_minor,refunded_minor,currency}]}`.
   Coverage and merchant authenticity are operator prerequisites, not
   established by JSON fields. No token, signatures or IDs go into output.
   Compare orders/accounts, confirmation/refund amounts, receipts, grant and
   credit ledgers, reservations, balances and duplicate effects. An unmatched
   provider operation blocks sales. Recover original invoice/account mapping
   and reconcile via the canonical verified idempotent callback, then repeat
   the gate; never issue manual compensating grants from this procedure.
   Before reopening also reconcile post-snapshot deletions and payment events
   through the actual reopening window. Neither gate nor restore switches traffic.
4. The gate reports measured snapshot ages for the selected state. Design
   intervals are 15 minutes for SQLite and 24 hours for media, **plus**
   time to complete verification/publication. With 12 min and 3 h job
   deadlines respectively, healthy worst windows are about 27 min for SQLite
   and 27 h for media (plus scheduler jitter). A missed run, unproven media
   or a fallback makes them longer. A 15-min checkpoint does not guarantee
   financial recovery: acknowledgements after the snapshot or missing order
   mappings require external reconciliation; media and internal spend can
   still be missing. Measure RPO/RTO in the pilot, not on production.
