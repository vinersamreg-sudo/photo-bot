# Ravuna RU cutover runbook — prepared, not deployed

Use an independent Ravuna VPS, not TripDay. Pilot candidate: Selectel VDS
4-8-80, Moscow ru-2c, 4 vCPU / 8 GB / 80 GB local NVMe with public IPv4.
Minimum 2/4/50 only if the tested restore scratch/headroom fits. No purchase, route
switch, data movement or production job is implied. Keep Hetzner backup and
OpenAI Image settings unchanged; exclude personal VPN and other services.

## Closed staging

1. Install Ubuntu 24.04, Python 3.12, OpenSSL, nginx and AWS CLI v2 with
   conditional PutObject support on an authorized staging host. Pin code and
   dependencies and record the exact verified source SHA in .deploy-sha;
   use /opt/photo-bot, user photoapp, private data/logs/temp and
   the existing ops/photo-bot.service. Do not enable units automatically. Use
   ops/ru/core.env.example with mode 0600, public access, AI requests,
   payments, callback listener and retention off. Use separate test secrets.
2. Prepare both immutable sites and canonical site/nginx vhosts, with CSP,
   valid TLS and unchanged Robokassa routes. Do not install Avito snippets.
   Provision a separate staging webhook hostname and full-chain certificate;
   substitute it in ops/ru/max-webhook.conf and the health unit. Test nginx
   before use. Listen on public 443, allowlist management SSH. The loopback
   8092 port receives MAX; 8091 is the existing private payment listener. Do
   not serve SQLite, private files or health endpoints through nginx.
3. Use a separate MAX test bot. Verify /me, GET subscriptions, verified TLS,
   buttons, signed media CDN downloads and preview/original uploads from the
   chosen RU IP. Explicitly set webhook transport, disable observe-only,
   keep public access off and supply a dedicated strong webhook secret.
   Configure the closed mock provider before starting handlers; no live key.
   Subscription changes are operator actions, not automatic. Verify ACK only
   after inbox commit, duplicate deliveries, HTTP 503 when commit is blocked,
   and 48–72 h of direct operation. The private /readyz detects slow queues
   and recent failed/uncertain events. Unclaimed queued events survive restart;
   bounded retry is permitted before the application claim. Uncertain claimed
   work is quarantined rather than repeated. Normalized inbox payloads are
   cleared after completion or 15 minutes; dedup receipts last seven days.
4. Run ravuna-ai-mock.service in isolation with a dedicated test CA, server
   certificate with the private IPv4 in its IP SAN, and client certificate with DNS SAN
   ravuna-core.internal. Bind to loopback/private tunnel and allowlist only
   the peer IP. Add the private origin and CA/cert/key file paths to the closed
   RU settings, with no OpenAI key. No public or paid mode may select the mock.
   Inputs: two 15 MiB images, 41 MiB envelope, one worker. Cache only the most
   recent response in memory for 90 seconds. Persist only opaque operation IDs;
   retrying an uncertain request cannot rerun it. The production OpenAI route
   is deliberately not exposed by the executable.

## Recovery

Use an independent private RU S3 bucket with no object versions, single writer
and separate reader, dedicated HTTPS endpoint, and no public ACLs. Never use an
existing shared/Hetzner bucket. Configure RU_S3_ENDPOINT, RU_S3_BUCKET and
RU_S3_REGION in /etc/ravuna/ru-recovery.env. Provision secrets using systemd
credentials and a private HTTPS alert receiver. Verify conditional catalog PUT
and multipart abort support; set lifecycle only for incomplete multipart
uploads (one or two days), not expiry of completed objects. Do not share writer credentials
between hosts. Verify region, contracts, cost and privileges on the chosen store.
Install the isolated CLI config from `ops/ru/aws-recovery.config` as
`/etc/ravuna/ru-s3-cli.conf` on both hosts. 64 MiB multipart chunks keep routine
write requests below Yandex's allowance at the modeled load; verify real
request counts, especially if SQLite reaches the multipart threshold.

The 03:00 Europe/Samara timer publishes only after full remote readback,
SHA256, restore, FK checks and commerce verification. Scheduled evidence needs
a recent timer trigger. Manual bootstrap can fill at most two distinct fallback
points and cannot advance the schedule. Keep the last four successful daily
labels and the latest successful label in each of the two completed Samara
calendar weeks (Monday–Sunday). Overlap is one object; equal verified DB/media
content is one object even if ciphertext differs. Six published slots plus
one fixed candidate bound storage. At least two distinct verified points are
required for healthy recovery; a brand-new bucket cannot report PASS for the
independent watchdog before this. A failed upload, readback, restore or CAS
never removes a published point. A verified replacement and durable CAS
precede retiring old slots. Use a single writer host and separate reader.
Never reinitialize a failed/unknown catalog. Reconcile obsolete slots and
alerts on the pilot; do not apply object-expiry rules to complete objects.

On an independent RU runner, the 06:05 watchdog reads the actual scheduled
ciphertext, validates SHA256, decrypts and restores all required files and SQLite,
checks FK and commerce, and compares verified content identity. It needs its own
read-only S3 credentials, separately provisioned recovery passphrase, encrypted
private scratch and alert receiver. The writer additionally rechecks the newest
previous point (a second fallback read may be needed if content is identical).
Minimum scratch free space 16 GiB and ciphertext cap 8 GiB remain unchanged.
For exports/restores, schema 16 and 17 archives use their original schema for
verification; older formats are not implicitly imported. Verify an isolated
copy before migrating its schema, never overwrite live data. Preserve all
existing Hetzner recovery points until separately authorized.

### SQLite between full recoveries

Proposed separate 15-minute unit: use SQLite online backup API (committed WAL
pages included without copying live WAL/SHM), encrypt, upload, fully read back,
verify SQLite/FKs/commerce and conditionally promote under `ravuna/db/`.
Two verified slots plus one candidate, separate writer credential, separate
passphrase, 128 MiB ciphertext cap. Neither catalog controls the other.
Use the independent read-only `ravuna-ru-db-watchdog.timer` every 15 min
(stale after 30 min), with separate reader credentials, and prove actual
alert delivery. A `restore` is into a new private
scratch file, never the production SQLite path. Goals, not guarantees:
DB RPO <=15 min plus completion, media RPO <=24 h plus completion, full RTO <=4 h.
A newer DB is not automatically safe with older media. Recover corresponding
later media or prove all references before promotion; otherwise stop, use a
coherent full point and reconcile.

Before reopening after a crash, fence both runtimes, reconcile all provider
invoices since the oldest candidate snapshot plus overlap, including orders
created after the last checkpoint. Compare payment statuses, receipts, one
grant per invoice, consumption, reversals and balances. Recover original
checkout/order mapping and use the existing verified idempotent ResultURL
path when possible. If mapping is missing or an acknowledged payment cannot
be reconstructed, keep sales closed; no improvised grants. A 15-minute copy
is not zero-loss payment recovery. Validate independent acquisition of order
mapping/provider records; consider a separately approved durable event journal
if that gate cannot be met.

### Cost at observed archive size

[12 November off-site run](https://github.com/vinersamreg-sudo/photo-bot/actions/runs/43149493291)
published a 3,883,894,882-byte artifact (3.617 GiB). This is artifact size, a
sizing proxy for its encrypted media payload; no new production copy or private
download. At that size: 21.70 GiB steady for six unique archives, up to 25.32
GiB at seven slots. Three full reads/day (candidate, previous, independent
restore) give ~325.54 GiB in 30 days. At observed SQLite size 44.53 MiB,
96 checkpoints/day give another ~125.24 GiB of readback, plus up to 0.13 GiB
storage, excluding occasional repeats and growth. [Yandex Standard](https://yandex.cloud/ru/docs/storage/pricing):
~437 ₽/mo for full points alone, ~647 ₽ with checkpoints, ~1,747 ₽ with 1,100 ₽
VPS (or ~2,347 ₽ including 600 ₽ assumed independent runner). Budget includes
7 full slots and 3 checkpoint slots, and does not count snapshot copies on VPS.
[FirstVDS S3](https://firstvds.ru/services/s3) advertises 250 ₽/mo for 100 GB
without traffic charges; use only after confirming conditional catalog writes,
multipart abort, scoped reader, legal RU location and complete restore. Then
VPS+S3 ≈1,350 ₽/mo before the runner. Recalculate from actual monthly bytes after
the pilot; do not count avoided storage before historic obligations are resolved.

Consider incrementals only after the full scheme passes: independently
encrypted, content-addressed media chunks plus a self-contained manifest and a
full SQLite snapshot at each point. Measure compressed unique bytes and read
cost on synthetic data. Reference-count manifests, protect the last two
verified manifests and their complete transitive object sets, rehearse missing
chunks, key rotation and full restore before approving garbage collection.
Do not replace full copies with a fragile delta chain or enable this now.

Acceptance: two consecutive plan-triggered runs, full S3 readback and restore,
and a missed schedule with a successful manual run still producing a delivered
alert. In addition simulate at least two calendar-week transitions, checkpoint
corruption, stale monitoring and repeated payment callbacks. Before GO, prove
an independent way to recover acknowledged payments, grants, and post-snapshot
consumption. Reapply verified deletion/expiry records before exposing any
restored gallery, especially older archives.

## Data export and cutover

1. Verify source SHA, schema, recovery proof, scopes of all writers, intended
   hostnames, TLS and callback windows. Prepare separate secret custody, the
   matching /opt/photo-bot layout (including old stored media paths) and an empty
   fenced RU target. Do not copy the source .env or neighboring services.
2. Before changing traffic, establish single durable RU payment intake or prove
   provider retries and reconciliation over the full DNS/cache/fence window.
   Preserve the canonical pixoraai.ru/payments/robokassa/result host/path,
   invoice sequence, checkout token configuration and signing credentials.
   No successful acknowledgment before acceptance; never grant from browser
   SuccessURL or manually increment a balance. Any unrecoverable late callback
   blocks cutover.
3. Under GO, close new work/sales, drain active jobs, reservations and the
   MAX inbox, then fence all writers. The export checks the instance lock and
   active DB state; operator must verify the broader fence (including cron and
   any separate payment listener). Stage to a private existing directory outside
   the app tree. Example command:

       python -m app.ru_recovery export --writers-fenced --root /opt/photo-bot --scratch /var/lib/ravuna-cutover --passphrase-file /run/credentials/recovery.pass

   Copy only the encrypted archive over authenticated transport and verify
   the expected SHA256 via a separate channel. On the destination:

       python -m app.ru_recovery restore --root /opt/photo-bot --scratch /var/lib/ravuna-cutover --archive /var/lib/ravuna-cutover/ARCHIVE --sha256 VERIFIED_HASH --destination /var/lib/ravuna-cutover/verified-tree --passphrase-file /run/credentials/recovery.pass

   The canonical online backup API captures committed WAL pages without copying
   live WAL/SHM or checkpointing the source. Restore validates ciphertext,
   manifest and each private file, schema/FK and finance before installing
   anything. Compare counts and commerce fingerprints. Promote only the verified
   database/photo_bot.sqlite3 and private_storage/users into fenced empty
   data/photo_bot.sqlite3 and data/users with private ownership. Keep archive
   and verified-tree local to the scope; no private paths or payloads in logs.
4. Confirm old checkout URLs, late/replayed ResultURL, balances, all three
   packages, direct MAX, gallery files and recovery; then switch DNS and the
   subscription under GO with exactly one active writer. Smoke without charging.
5. Roll back to the unchanged source only **before the first RU write** and
   after proving no callback was acknowledged on the RU side. After a write,
   fence the RU writer, recover the authoritative RU state to a RU standby or
   forward-fix, reconcile invoices and resume exactly one writer. Never revive
   a stale Hetzner DB or copy new localized data back to Germany by default.

## Risk register / NO-GO

- OpenAI and personal data: an overseas IP or mTLS is not evidence of provider
  permission or lawful transfer. No outreach, filing, or real images are
  authorized. Keep the gateway mock-only until the actual operator, end-user
  route and handling of prompts/images are permitted and documented.
- MAX: direct routing, subscription and media from the selected RU network are
  not proven by local mocks. Disable only Ravuna's Aéza link by separate GO
  after stable live operation; do not touch personal services.
- Finance: unresolved callback-window coverage or ledger discrepancy blocks
  switching. The production merchant and payment code are not changed.
- Recovery: no launch before independent RU storage, full restore, scheduling
  and actual alert delivery are demonstrated. No current backup is removed.
