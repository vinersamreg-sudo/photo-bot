# Ravuna RU cutover runbook — prepared, not deployed

Use an independent Ravuna VPS, not TripDay. Initial size: 2 vCPU / 4 GB /
100 GB NVMe and stable Russian public IPv4. No infrastructure purchase, route
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

The 03:00 Europe/Samara timer publishes only after full remote readback, SHA256,
restore, foreign-key checks and commerce verification. Scheduled evidence also
requires a recent systemd timer trigger in its start window. Manual runs omit
the scheduled flag. The 06:05 watchdog checks the scheduled proof and reads back both
verified points. For production put this one daily watchdog on an independent
RU runner, with read-only credentials and its own alert delivery, to detect
total VPS loss without double daily watchdog traffic. The backup
retains two catalog entries and uses one fixed candidate slot; a failure cannot
delete either currently published point. Only after conditional catalog commit
can the retired slot be removed. Keep encryption at rest for local scratch and
restore storage, with >=16 GiB free and an 8 GiB candidate limit. Preserve
all existing Hetzner recovery points until separately authorized.

Acceptance: two consecutive plan-triggered runs, full S3 readback and restore,
and a missed schedule with a successful manual run still producing a delivered
alert. Targets, not proven: full RPO <=24 h plus completion, RTO <=4 h. Two
daily copies do not give zero-loss commerce. Before GO, prove an independent
way to recover acknowledged payments, grants, and post-snapshot consumption.

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
