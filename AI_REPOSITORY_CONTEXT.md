# AI Repository Context

Repository: `photo-bot`; product brand: Pixora; production root: `/opt/photo-bot`.

Read first: `docs/PROJECT_BIBLE.md`, `CURRENT_STATE.md`, `DECISIONS.md`, `ARCHITECTURE.md`, `UX_COPY_GUIDE.md`, `LAUNCH_READINESS.md`, `PILOT_PLAN.md`.

Current v1 contract:

- OpenAI `gpt-image-2` is the only production image provider;
- do not add semantic parser, second provider or multi-provider abstractions without new owner decision based on pilot evidence;
- main MAX flow is `/start → photo → free text → processing → watermarked result`;
- no scenario menu before photo, no «Своя идея», no prompt confirmation;
- «Идеи» is optional; correction/repeat preserve lineage; Gallery is user-facing;
- production defaults to observe-only and pilot limit 0;
- never make real OpenAI image calls without explicit agreed maximum;
- never expose user IDs, tokens, prompts, private paths or originals in reports/logs.
- migration v9 contains the permanent `continuation_pack_2_plus_1` ledgers; all real-payment flags are forced off by deploy;
- a verified 49 ₽ ResultURL atomically grants two generation credits and one user-selected original entitlement; it never auto-selects a version;
- the entitlement may unlock one owned available GalleryVersion created before or after purchase; MAX delivery failure preserves unlocked state and allows re-delivery;

Latest production evidence (17.07.2026): owner-only E2E used exactly 5/5 approved
real image requests; all succeeded and created GalleryVersions 11–15. Corrections,
Repeat, History, Favorite and Current best were exercised. Production is restored
to observe-only with pilot limit 0, processing 0, SQLite quick_check ok and orphan
private files 0. Read `docs/AI_BRAIN_VISUAL_VALIDATION.md` and
`docs/CURRENT_STATE.md` before proposing more image calls or parser work.

Operations:

- `python -m app.main health`
- `python -m app.main launch-status [--strict]`
- `python -m app.main maintenance-cleanup [--execute]`
- `python -m app.main backup-create --passphrase-stdin`
- `python -m app.main backup-restore-test --backup NAME --passphrase-stdin`

Migration v6 contains privacy-minimal product events; migration v8 preserves legacy payment audit state; migration v9 adds global generation-credit lots/reservations/ledger, package grants and original entitlements. Backups are encrypted and readiness requires a real restore plus off-site artifact. Payment architecture is implemented but no provider sandbox/real-payment evidence or final legal approval exists.

Official website context (20.07.2026): `site/public` is the only site implementation. The verified MAX deep link is `https://max.ru/se13572368_bot`; the permanent product is «49 ₽ — ещё два варианта и один выбранный original без водяного знака»; the public copy truthfully says closed testing and unavailable payment. Site deploy is separate and atomic under `/opt/pixora-site`; repository gate `PIXORA_SITE_DEPLOY_ENABLED=true` was opened only after the launch checks passed. Apex and `www` resolve to `116.203.24.102`; trusted HTTPS, canonical redirects, HSTS, browser smoke and Certbot renew dry-run pass. Owner-supplied seller data is published: INN `631937938795` and e-mail `viner-89@mail.ru`. This closes the engineering site gate for Robokassa moderation, but does not authorize sandbox, production payments or public bot handlers.

Commercial operations: `payment-status`, `payment-history`, `payment-show`, `payment-reconcile`, dry-run-first `payment-resend-original`, `payment-mark-delivery-retry`, `refund-create`, `refund-history/status`, `robokassa-health`, `pilot-status/report`, storage/backup/cleanup/cost and `health-report`. Read `docs/PAYMENTS.md`, `ROBOKASSA.md`, `PAYMENT_GO_LIVE_AUDIT.md`, `ROBOKASSA_CABINET_SETUP.md`, `ROBOKASSA_SANDBOX_E2E.md`, `PAYMENT_SUPPORT_RUNBOOK.md`, `PILOT_5_USERS_RUNBOOK.md` and `PILOT_UNIT_ECONOMICS.md`. ResultURL is POST-only. Never enable sandbox, production, public proxy or accept real money without the corresponding separate owner authorization.

Current commercial gate (20.07.2026): site may be submitted for Robokassa moderation; public ResultURL and sandbox evidence do not exist; real payment/refund/reconciliation are unverified; handlers are observe-only and pilot limit is 0. `launch-status` reports those gates separately and always keeps `public_launch_ready=false`. The 49 ₽ economics are viable only under strict generation-cost/conversion assumptions; consult the executed notebook before changing quota or price.

Content Studio context (21.07.2026): `app/content_studio` is an independent
operator-only demonstration-content subsystem with separate SQLite/storage. It
accepts only created-for-Pixora, commercially verified sources; renders three
Before/After layouts, generates factual Russian copy from deterministic templates,
and enforces mandatory demo disclosure, CTA/UTM and manual review. Production
publishing is forced off, no transport is configured, and no real content or AI
request was created in its implementation sprint. Use `scripts/pixora content ...`.
Before importing the first real asset, add and restore-test encrypted off-site
backup of the Content Studio database and storage. Read the four `CONTENT_*`/
`DEMO_CONTENT_POLICY.md` documents before changing this boundary.

AI quality limits: identity/background can drift, corrections accumulate changes, outputs are nondeterministic and not pixel-perfect Photoshop. Report these honestly.

Optional OpenAI context (19.07.2026): migration v7 and adapters exist but all
feature flags are false in production. Pixora memory remains SQLite + Storage +
GalleryVersion + SceneIntent. `previous_response_id` is auxiliary per-version
lineage; Repeat is stateless; failures fall back to `/v1/images/edits`. No real
image request was made for this implementation. Before enabling anything, read
`docs/OPENAI_CONVERSATION_MEMORY_AUDIT.md`, architecture and four-call runbook.
New operation: `python -m app.main provider-context-cleanup [--execute]`.
