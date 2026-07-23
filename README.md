# photo-bot

Официальный бренд продукта — **Pixora**. Отдельный статический лендинг находится в [`site/`](site/README.md); он не импортирует backend и имеет собственный CI/deploy boundary.

## Content Studio

`app/content_studio` — независимый операторский контур для честных
демонстрационных материалов официального MAX-канала. Он импортирует только
созданные для Pixora before/after изображения с подтверждённым коммерческим
разрешением, собирает три формата карточек через Pillow, создаёт русский текст из
детерминированных шаблонов и ведёт review-first очередь. Он не читает Gallery,
платежи или пользовательские диалоги и не вызывает OpenAI.

```powershell
scripts\pixora.ps1 content status
scripts\pixora.ps1 content queue --status needs_review
scripts\pixora.ps1 content schedule --plan-start 2026-07-20 --days 7
```

Production deploy принудительно оставляет
`CONTENT_STUDIO_PUBLISHING_ENABLED=false`; доступны только локальные preview и
dry-run. Архитектура, политика и runbook: [Content Studio](docs/CONTENT_STUDIO_ARCHITECTURE.md),
[pipeline](docs/CONTENT_PIPELINE.md), [MAX strategy](docs/MAX_CONTENT_STRATEGY.md),
[demo policy](docs/DEMO_CONTENT_POLICY.md).

## AI Brain

Pixora не передаёт correction как голую строку. `app/edit_intent.py` строит сериализуемый EditPlan v2 с provider-neutral scene fields; `app/prompt_builder.py` создаёт только English/ASCII technical prompt. Correction редактирует private original выбранной успешной версии и меняет только затронутые поля, Repeat сохраняет тот же intent и input branch. Raw Russian text никогда не передаётся ImageProvider. Подробности: [аудит до изменений](docs/AI_BRAIN_AUDIT.md), [архитектура](docs/AI_BRAIN_ARCHITECTURE.md), [правила prompt](docs/PROMPT_ENGINEERING_RULES.md).

Безопасный административный просмотр intent/provider prompt:

```bash
python -m app.main ai-inspect --attempt-id <opaque-attempt-id>
```

Команда не выводит platform user ID, файловые пути, ключи или исходный пользовательский free text.

Личная AI-фотостудия: все работы пользователя, их версии и избранное живут в одном месте. `photo-bot` — техническое имя, публичный бренд — Pixora. Pixora v1 использует только OpenAI `gpt-image-2`; экспериментальные hybrid-processing модули выключены. Доступ закрыт allowlist: owner и управляемые этапы 5/10/20 пользователей. Обычный deploy всегда возвращает `MAX_POLL_OBSERVE_ONLY=true` и pilot limit 0.

## MAX production smoke

Безопасные административные команды:

```bash
python -m app.main max-check   # GET /me, без polling и сообщений
python -m app.main health      # SQLite + systemd + MainPID + fresh MAX contact + lock
```

Runtime запускается только как `/etc/systemd/system/photo-bot.service` под `photoapp`. Long Polling допускается для ограниченного пилота, но не считается архитектурой публичного массового запуска.

## Быстрый старт

Требуется Python 3.12.

```powershell
cd C:\Users\viner\Documents\Codex\photo-bot
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m unittest discover -v
python -m app.main health
```

Для локального запуска укажите в `.env` локальный `BASE_DIR`. Реальный ключ хранится только в локальном `.env`, GitHub Environment `production` и production `.env`; он никогда не коммитится.

## Demo vertical slice

Локальная проверка без внешнего API:

```powershell
python -m app.main demo-edit --user-id test-user --image path\to\image.jpg --prompt "Сделай светлый фон для резюме" --provider fake
python -m app.main demo-stats
```

Без `--provider fake` команда использует настроенный OpenAI `images.edit`. Она выводит только путь к уменьшенному watermarked preview. Оригинал хранится отдельно в `data/users/<opaque-id>/demo_sessions/<session-id>/originals` и не входит в пользовательский ответ.

Начальный баланс относится к MAX user ID, а не к demo-сессии или фотографии: пользователь однократно получает две успешно доставленные обработки и может распределить их между разными снимками. Технические и policy-ошибки, отмена и ошибка доставки баланс не расходуют.

## Personal studio API

Каждая успешная попытка автоматически становится новой версией одной работы Gallery. `app/gallery.py` предоставляет операции для repeat/correction, before/after metadata, favorites/current best, collections, tags, preferences, search, recent/continue, trash и restore. Заблокированный original никогда не возвращается пользовательским DTO.

Новые работы используют `data/users/<opaque-user-id>/gallery/<item-id>`. Существующие demo-файлы не перемещаются: migration v2 создаёт ссылки на них и сохраняет обратную совместимость.

```powershell
python -m app.main gallery-cleanup           # dry-run
python -m app.main gallery-cleanup --execute # физическая очистка
python -m app.main maintenance-cleanup       # retention + temp + orphan dry-run
python -m app.main launch-status              # privacy-safe readiness
python -m app.main health-report              # consolidated commercial health
python -m app.main payment-status             # no identifiers or secrets
python -m app.main pilot-status
python -m app.main cost-status
```

Retention задают `DEMO_RETENTION_DAYS=30`, `PAID_RETENTION_DAYS=180` и `TRASH_RETENTION_DAYS=30`. MAX показывает последние работы как preview-карточки, версию, correction/repeat/current-best/favorite/delete. Collections, сложный поиск, before/after slider и экспорт не входят в основной v1 UX.

## MAX closed-test transport

Официальный API contract и результаты поиска старого бота описаны в [MAX_TRANSPORT_AUDIT.md](docs/MAX_TRANSPORT_AUDIT.md). Реализованы owner allowlist, закрытый ответ для остальных и прямой UX `/start → фото → текст → результат`. Загрузка валидного source автоматически фиксирует согласие; отдельного Continue и prompt confirmation нет. Следующий текст после результата автоматически продолжает работу как correction. «✨ Идеи» — необязательный каталог. Processing, watermarked preview, correction/repeat, «Мои работы», favorite, корзина и restore сохранены.

## Commercial payments

Публичный цифровой продукт называется **«Пакет доступа Pixora»** и стоит 49 ₽. После подтверждения оплаты пользователю начисляются две обработки и один оригинал без водяного знака. Внутренний код `continuation_pack_2_plus_1`, credit ledger и entitlement ledger не меняются: платёж не выбирает версию автоматически, entitlement можно применить к любой доступной собственной версии, созданной до или после покупки. Повторные пакеты складываются. ResultURL атомарно начисляет обе части пакета и остаётся идемпотентным. Реальные деньги не включены: каждый deploy принудительно оставляет payment/webhook/refund flags off, sandbox mode и no production approval. Read [Payments](docs/PAYMENTS.md), [Robokassa](docs/ROBOKASSA.md), [architecture](docs/PAYMENT_ARCHITECTURE.md), [security](docs/PAYMENT_SECURITY.md) and [launch runbook](docs/COMMERCIAL_LAUNCH.md) before changing any commercial flag.

Read-only and dry-run-first operator tools:

```bash
python -m app.main payment-show --invoice <invoice> --format human
python -m app.main payment-reconcile [--invoice <invoice>] --format human
python -m app.main payment-resend-original --invoice <invoice> [--apply]
python -m app.main payment-mark-delivery-retry --invoice <invoice> [--apply]
python -m app.main refund-create --invoice <invoice> --amount-rub 49 --reason customer_request --idempotency-key <ticket> --dry-run
python -m app.main robokassa-health --format human
python -m app.main pilot-report --format human
python -m app.main credit-status --platform-user-id <MAX-ID>
python -m app.main credit-history --platform-user-id <MAX-ID>
python -m app.main entitlement-status --platform-user-id <MAX-ID>
python -m app.main package-status --platform-user-id <MAX-ID>
python -m app.main credit-adjust --platform-user-id <MAX-ID> --delta 1 --reason <ticket> --idempotency-key <key> # dry-run
```

Owner procedures: [moderation package](docs/ROBOKASSA_SUBMISSION_PACKAGE.md), [payment audit](docs/PAYMENT_GO_LIVE_AUDIT.md), [cabinet map](docs/ROBOKASSA_CABINET_SETUP.md), [sandbox E2E](docs/ROBOKASSA_SANDBOX_E2E.md), [payment support](docs/PAYMENT_SUPPORT_RUNBOOK.md), [five-user pilot](docs/PILOT_5_USERS_RUNBOOK.md), [unit economics](docs/PILOT_UNIT_ECONOMICS.md).

```powershell
python -m app.main max-check
python -m app.main run
```

`run` запускает MAX только при `MAX_TRANSPORT_MODE=polling` и настроенном `MAX_BOT_TOKEN`. Polling разрешён исключительно для разработки/закрытого smoke. В `disabled` процесс завершается, а `webhook` не запускается до появления HTTPS:443 endpoint. Original не входит ни в один transport response.

## Production

- VPS: Hetzner `ai-prod-01`, Ubuntu 24.04 LTS, Python 3.12;
- приложение: `/opt/photo-bot`;
- пользователь приложения и деплоя: `photoapp` без `sudo`;
- Python: `/opt/photo-bot/venv/bin/python`;
- состояние: `data/`, логи: `logs/`, временные файлы: `temp/`;
- конфигурация: `/opt/photo-bot/.env`, права `600`.

Push в `main` запускает тесты и деплой через GitHub Actions. Деплой синхронизирует только этот проект и сохраняет `.env`, `venv/`, `data/`, `logs/`, `temp/`. TripDay не является частью этой системы и не используется.

Ручной запуск доступен через **Actions → Test and deploy → Run workflow**. Операционные команды:

```bash
/opt/photo-bot/scripts/healthcheck.sh
/opt/photo-bot/scripts/start_bot.sh
/opt/photo-bot/scripts/stop_bot.sh
```

Systemd hardening template находится в `ops/photo-bot.service`. Ежедневный workflow создаёт encrypted SQLite backup, реально восстанавливает его, копирует off-site и только затем выполняет cleanup. Код backup без зелёного restore workflow не считается доказательством готовности.

## Official website

Официальный продуктовый сайт находится в `site/public` и развёртывается отдельно от бота в `/opt/pixora-site`. Он честно описывает закрытое тестирование, ведёт в проверенный MAX-бот и фиксирует единый цифровой продукт: «Пакет доступа Pixora» за 49 ₽, включающий две обработки и один оригинал без водяного знака. Платежи остаются выключенными.

Site workflow закрыт переменной production environment `PIXORA_SITE_DEPLOY_ENABLED`. Её нельзя включать до переноса DNS на Hetzner, выпуска доверенного TLS-сертификата, внешнего HTTPS smoke и подтверждения неизменности backend. Начинать с [site README](site/README.md), [production runbook](docs/SITE_PRODUCTION_RUNBOOK.md), [TLS runbook](docs/TLS_CERTIFICATE_RUNBOOK.md) и [Robokassa checklist](docs/ROBOKASSA_SITE_MODERATION_CHECKLIST.md).

## Документация

Главный источник истины — [docs/PROJECT_BIBLE.md](docs/PROJECT_BIBLE.md). Текущее состояние, архитектура, процесс поставки и решения описаны в остальных файлах каталога `docs/`.

Исторические hybrid-processing исследования сохранены в `docs/`, но не описывают production path v1. Повторная оценка provider/semantic parsing проводится только после данных закрытого пилота.

## Optional OpenAI context (disabled)

Pixora хранит память работы сама, а OpenAI conversation используется как
дополнительный контекст для последовательных правок. Интеграция закрыта тремя
feature flags; обычный deploy устанавливает их в `false`. Текущий production
endpoint остаётся `/v1/images/edits`.

Аудит и lifecycle: [OPENAI_CONVERSATION_MEMORY_AUDIT.md](docs/OPENAI_CONVERSATION_MEMORY_AUDIT.md),
[OPENAI_CONVERSATION_MEMORY_ARCHITECTURE.md](docs/OPENAI_CONVERSATION_MEMORY_ARCHITECTURE.md),
[PROVIDER_CONTEXT_LIFECYCLE.md](docs/PROVIDER_CONTEXT_LIFECYCLE.md).

```bash
python -m app.main provider-context-cleanup
python -m app.main provider-context-cleanup --execute
```
