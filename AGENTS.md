# Ravuna agent rules

Read this file before changing or diagnosing the repository. It is the mandatory
entry point for Codex, Claude, and other coding agents. Chat history is not a
source of truth; use the repository and the current production snapshot.

## Product

- Active brand: **Ravuna**.
- Ravuna is a commercial AI photo editor delivered through a MAX bot.
- The official public site is `https://ravuna.ru`.
- The application repository and systemd service retain the technical name
  `photo-bot`; do not rename internal identifiers merely for branding.
- The 49 ₽ public product remains “Пакет доступа Ravuna”: two photo edits and
  one original without a watermark. Ravuna also offers a one-time 1990 ₽
  package with 100 edits and 50 originals; neither package is a subscription.
- The repository default image provider is Google Gemini Nano Banana Pro using
  `gemini-3-pro-image`; OpenAI `gpt-image-2` remains a selectable option.
- Robokassa confirms payment through the server-side ResultURL.
- SQLite and private filesystem storage are the production persistence layer.

## Mandatory first steps

1. Read this file.
2. Read `AI_REPOSITORY_CONTEXT.md`.
3. Read only the relevant document from `docs/current/`.
4. Inspect the exact code and tests in scope.
5. Check `git status --short` and preserve unrelated changes.
6. Confirm the requested test tier before expensive or external actions.

Do not re-read the whole archive for routine work. Use `docs/archive/` only for
historical evidence or when a current document links to a specific record.

## Repository map

- `app/main.py`: command-line entry point, health and operator commands.
- `app/max_application.py`: MAX product flow and callback handling.
- `app/max_transport.py`: MAX Bot API transport and polling.
- `app/provider_router.py`: configured image-provider selection.
- `app/image_provider.py`: OpenAI adapter plus provider abstraction and test provider.
- `app/gemini_image_provider.py`: Google Gemini image-edit adapter.
- `app/openai_client.py`: OpenAI image edit integration.
- `app/direct_prompt.py`: exact Unicode passthrough for Gemini and optional
  deterministic handling for non-Gemini direct paths.
- `app/edit_intent.py`: deterministic intent extraction.
- `app/prompt_builder.py`: technical prompt construction.
- `app/gallery.py`: works, versions, lineage, favorites and current best.
- `app/demo_service.py`: generation lifecycle, previews and watermarking.
- `app/commerce.py`: credits, entitlements and commercial grants.
- `app/payments.py`: payment intent/order lifecycle.
- `app/robokassa.py`: Robokassa signatures and payment URLs.
- `app/payment_webhook.py`: ResultURL transport.
- `app/database.py`: SQLite schema and migrations.
- `app/storage.py`: private file layout and safe file operations.
- `app/operations.py`: readiness and privacy-safe operational reports.
- `site/public/`: published Ravuna static site.
- `site/tests/`: site and legal-page checks.
- `scripts/`: operator, release and diagnostic tools.
- `ops/`: systemd, nginx and trusted certificate assets.
- `.github/workflows/`: tests, deploy, site, backup and bounded E2E workflows.
- `tests/`: `unittest` product and infrastructure tests.

## Production topology

- VPS: Hetzner, Ubuntu, application root `/opt/photo-bot`.
- Runtime user and deploy user: `photoapp`.
- Python: `/opt/photo-bot/venv/bin/python`.
- Service: exactly one `photo-bot.service` systemd runtime.
- Runtime configuration: `/opt/photo-bot/.env`, mode `600`.
- Mutable state: `/opt/photo-bot/data`, `logs`, and `temp`.
- Site root: `/opt/ravuna-site/current` behind nginx and HTTPS.
- ResultURL is served at the established `pixoraai.ru` callback URL for
  compatibility. Do not migrate that URL without a separate payment project.

## Approved production baseline

Production is a public, working commercial service. The approved normal state is:

- `MAX_PUBLIC_ACCESS_ENABLED=true`;
- `MAX_POLL_OBSERVE_ONLY=false`;
- `MAX_TRANSPORT_MODE=polling`;
- `PAYMENTS_ENABLED=true`;
- `PAYMENT_PROVIDER=robokassa`;
- `PAYMENT_WEBHOOK_ENABLED=true`;
- `PAYMENT_REFUNDS_ENABLED=false` unless separately approved;
- `ROBOKASSA_MODE=production`;
- `ROBOKASSA_PRODUCTION_APPROVED=true`;
- `OPENAI_IMAGE_REQUESTS_ENABLED=true`;
- `IMAGE_PROVIDER=gemini`;
- `GEMINI_IMAGE_MODEL=gemini-3-pro-image`;
- `IMAGE_DIRECT_PROMPT_ENABLED=true`;
- `IMAGE_SUBJECT_PRESERVE_GUARD_ENABLED=true`;
- `IMAGE_FACE_PRESERVE_GUARD_ENABLED=true`;
- `PILOT_USER_LIMIT=0`.

For Gemini/Nano Banana, the two preservation flags are compatibility settings
only: they must not append, prepend or otherwise change the provider prompt.
Gemini receives the exact original Unicode user prompt.

Always confirm the actual snapshot before a test or release. If it differs, treat
the actual state as evidence requiring investigation; never silently overwrite it
with this list.

## Deployment invariants

- An ordinary deploy must preserve the existing production operating state.
- Existing `.env`, `venv/`, `data/`, `logs/`, `temp/`, and live site state are
  preserved by deployment.
- Defaults are applied with `ensure_env`; they are only defaults for missing keys.
- A fresh or empty server must remain fail-closed:
  public access off, observe-only on, payments/webhook off, provider disabled,
  Robokassa sandbox and not production-approved.
- Post-deploy checks must verify the restored/retained state. They must not make a
  healthy public production look safe by closing it.
- Never change a working production state merely to make a test pass.
- Capture the complete relevant state before a temporary test and restore the
  exact snapshot afterward.
- Every temporary test must include `MAX_PUBLIC_ACCESS_ENABLED` in its snapshot.

## Product invariants

- Do not change the 49 ₽ package or its grant of two edits plus one original
  without explicit product approval.
- Do not change the 1990 ₽ package or its grant of 100 edits plus 50 originals
  without explicit product approval.
- Never grant from SuccessURL/FailURL browser redirects.
- ResultURL and ledger writes must remain idempotent.
- Duplicate callbacks must not duplicate credits, entitlements or receipts.
- Never expose an original before a valid entitlement is consumed.
- A provider, policy, validation or delivery failure must not consume an edit.
- A successful delivered edit consumes exactly one applicable edit allowance.
- Store the source, original, preview and version lineage consistently.
- Watermark only the preview, never the stored original.
- Corrections use the selected parent version, not an unrelated source.
- For Gemini/Nano Banana, never implement identity preservation by injecting
  hidden text, a guard, translation, intent parsing or prompt expansion. The
  provider prompt is exactly the original Unicode user prompt.

## Security and privacy invariants

- Never print, commit, report or echo tokens, passwords, IDs or private paths.
- If the project's confirmed monetization is below USD 1,000 per month, keys may be provided in the current task chat for an explicitly authorized operation.
- Chat-provided keys must still never be repeated in output or logs, committed, or passed through command-line arguments when stdin or a secret input is available; store them only in approved secret storage.
- At USD 1,000 per month or above, keys must not be provided through chat.
- Secrets belong only in local/production `.env` and GitHub Environment secrets.
- Do not put secrets in command-line arguments when stdin is available.
- Operator output must avoid user IDs, prompts, image bytes and payment secrets.
- Do not inspect private photos unless the task explicitly requires visual review.
- Do not use real customer data for tests.
- Use synthetic adult images for bounded visual validation.
- Do not expose SQLite or private storage through nginx.
- Do not follow symlinks during cleanup or file delivery.

## Permissions and external actions

- Read-only local inspection is allowed for diagnosis and review.
- Code/document changes require an implementation request.
- Commit, push, deploy, payment, OpenAI image request, browser mutation and user
  messaging require explicit authority from the current request.
- Never infer permission for a real payment or real image request from a test task.
- Do not change Robokassa cabinet, GitHub secrets, DNS or production flags unless
  the user explicitly places that action in scope.
- Do not shut down, restart or sleep the user’s laptop unless the user gives a
  direct command in the current task. A past instruction does not carry forward.

## Test tiers

### T0 — diagnose

- Read-only inspection only.
- No file changes, external requests, payments, image requests or deployment.
- Use narrow searches and existing reports.
- Report the confirmed cause and evidence.

### T1 — local targeted

- Change only the scoped implementation.
- Run the relevant fast profile or named test modules.
- Run `py_compile`/compile check for changed Python.
- Run `git diff --check`.
- No commit or deploy unless explicitly requested.

### T2 — extended local

- Run extended targeted tests.
- Run tracked-file secret scan.
- Run compile and diff checks.
- One intentional commit is allowed only when requested.
- No deploy unless explicitly requested.

### T3 — release

- Run the full release suite once.
- Create a backup when mutable paths, migrations or data handling change.
- Make one scoped commit and push.
- Deploy once and wait for CI.
- Verify SHA, health, database, processing, orphans and exact runtime flags.

### T4 — real end-to-end

Use only when the requirement cannot be proven below T4:

- Robokassa real/sandbox lifecycle;
- MAX transport and actual callbacks;
- external image generation/editing;
- private storage/original delivery;
- migrations and entitlement/ledger behavior.

T4 always requires an explicit request, a hard operation limit, a pre-snapshot,
an exact restoration plan and a final state verification.

## Test commands

Fast profiles:

```powershell
python scripts/test_fast.py max
python scripts/test_fast.py payments
python scripts/test_fast.py openai
python scripts/test_fast.py site
python scripts/test_fast.py storage
```

Named tests:

```powershell
python scripts/test_fast.py --test tests.test_max_adapter
python -m unittest -q tests.test_payments
```

Release suite:

```powershell
python scripts/test_release.py
```

Read-only production summary on the VPS:

```bash
python scripts/production_status.py --root /opt/photo-bot
```

The repository uses `unittest`. Do not introduce pytest or mass-convert tests
without a separate migration decision. Profiles are the routing mechanism.

## Choosing tests

- MAX callbacks/copy/state: `max` profile plus named conversation/application test.
- Payment/Robokassa/ledger: `payments` profile.
- Prompt/image providers: `providers` profile; OpenAI-only adapter work: `openai`.
- Static pages/legal/SEO: `site` profile.
- Gallery/storage/cleanup/original: `storage` profile.
- Cross-module or release-sensitive changes: T2 or T3.
- Never run the full suite repeatedly while debugging one assertion.
- On failure, rerun only the failed module/test until fixed.
- Run the complete release suite once at the end of T3.

## Output discipline

- Successful tools should print a 10–20 line summary, not full logs.
- On failure print the command, relevant tail and exact rerun command.
- Do not paste complete documentation, workflows or logs into chat.
- Use `rg` and narrow line ranges.
- Prefer counts, changed paths and status over raw output.
- Redact identifiers even when the user has posted them in chat.

## Git rules

- Inspect `git status --short` before editing and before staging.
- User changes and unrelated dirty files are not yours.
- Stage explicit paths; never use `git add -A` in a dirty repository.
- One sprint should normally produce one coherent commit.
- Do not amend, reset, checkout or discard user changes without permission.
- Do not push or deploy unless the request authorizes it.
- After push, wait for the relevant CI instead of starting duplicate runs.
- A documentation/tooling-only change does not require production deploy unless it
  changes runtime/deploy behavior or the user explicitly asks for deployment.

## Documentation routing

- Architecture and entities: `docs/current/ARCHITECTURE.md`.
- User flow and copy rules: `docs/current/UX.md`.
- Robokassa, grants and receipts: `docs/current/PAYMENTS.md`.
- Actual production baseline: `docs/current/PRODUCTION.md`.
- Release mechanics: `docs/current/DEPLOY.md`.
- Operator commands/incidents: `docs/current/OPERATIONS.md`.
- Accepted future work: `docs/current/BACKLOG.md`.
- Sprint handoff format: `docs/current/SPRINT_TEMPLATE.md`.
- Measured unit economics: `docs/current/ECONOMICS.md`.
- Privacy and security: `docs/current/SECURITY.md`.
- Historical evidence only: `docs/archive/`.

## Documentation update rules

- Update `PRODUCTION.md` when approved runtime flags/topology change.
- Update `DEPLOY.md` when workflow, rollback or post-deploy gates change.
- Update `PAYMENTS.md` when Robokassa, receipt, grant or refund behavior changes.
- Update `UX.md` when a user-visible state, message or button changes.
- Update `ARCHITECTURE.md` when module boundaries or entities change.
- Update `OPERATIONS.md` when an operator command or incident response changes.
- Update `ECONOMICS.md` only from measured or explicitly labeled estimated data.
- Add future work to `BACKLOG.md`; do not turn current docs into journals.
- Move superseded reports to `docs/archive/`; do not delete audit history.
- Do not update documentation for a change that did not actually happen.

## Правила поддержания контекста и документации

### 1. Источник истины

Источником актуального состояния проекта являются:

- Git repository;
- `AGENTS.md`;
- документы из `docs/current/`.

История чатов, старые переписки и старые отчёты не являются источником истины.

Перед началом любой новой задачи агент обязан:

1. прочитать `AGENTS.md`;
2. определить тип задачи;
3. прочитать только соответствующие документы из `docs/current/`;
4. не читать `docs/archive/` без прямой необходимости.

### 2. Правила обновления документации

После завершения крупного спринта обновлять только необходимый минимум:

- `AGENTS.md` — только если изменились правила работы, архитектурные принципы или
  процесс разработки;
- один профильный документ из `docs/current/` — если изменилось соответствующее
  поведение системы;
- `BACKLOG.md` — если появились новые задачи, технический долг или отложенные
  решения.

После каждого завершённого спринта агент должен проверить, требуется ли
обновление `AGENTS.md`, профильного документа из `docs/current/` или
`BACKLOG.md`. Если обновление не требуется, документацию не менять.

Не создавать новые документы, если информация относится к существующему
current-документу.

### 3. Запрет на большие отчёты после каждой правки

Не создавать длинные итоговые документы после каждой задачи.

Обычный спринт должен завершаться:

- коротким итоговым сообщением;
- изменением только необходимых документов;
- commit message.

Подробные расследования, аудиты и исторические отчёты помещать только в
`docs/archive/`, если они имеют долгосрочную ценность.

### 4. Разделение текущего состояния и истории

`docs/current/` содержит только актуальные правила и состояние проекта.

`docs/archive/` содержит:

- завершённые расследования;
- старые решения;
- исторические отчёты;
- предыдущие состояния проекта.

Не копировать историческую информацию обратно в current.

### 5. Принцип минимального контекста

При новой задаче агент должен использовать минимальный достаточный набор
информации.

Не читать:

- весь `docs`;
- весь Git history;
- старые чаты;
- архивные документы,

если задача не требует этого напрямую.

Цель: сохранить точность работы и снизить расход контекста.

## New-chat handoff

A new task should receive only:

1. repository path;
2. `AGENTS.md` instruction;
3. current production SHA/state if relevant;
4. one linked current document;
5. concrete objective and allowed test tier;
6. explicit external-operation limits.

Do not teach a new chat with a transcript dump. If required knowledge is missing,
repair the repository source of truth instead.

## Completion checklist

- Requested outcome is actually complete.
- No unrelated files changed or staged.
- Applicable test tier passed.
- Secret scan passed when required.
- `git diff --check` passed.
- Production was not changed without authority.
- If production was changed: SHA, health, SQLite, processing, orphans, runtime and
  exact flags were verified.
- Current documentation was updated only where required.
- Laptop shutdown was not performed without a direct current instruction.
