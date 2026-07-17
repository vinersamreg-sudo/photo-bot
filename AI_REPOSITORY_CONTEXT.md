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

Operations:

- `python -m app.main health`
- `python -m app.main launch-status [--strict]`
- `python -m app.main maintenance-cleanup [--execute]`
- `python -m app.main backup-create --passphrase-stdin`
- `python -m app.main backup-restore-test --backup NAME --passphrase-stdin`

Migration v6 contains privacy-minimal product events. Backups are encrypted and readiness requires a real restore plus off-site artifact. Payment and final legal documents are not ready. Site must not be published until MAX deep link and legal/public-launch decision are ready.

AI quality limits: identity/background can drift, corrections accumulate changes, outputs are nondeterministic and not pixel-perfect Photoshop. Report these honestly.
