# photo-bot

Официальный бренд продукта — **Pixora**. Отдельный статический лендинг находится в [`site/`](site/README.md); он не импортирует backend и имеет собственный CI/deploy boundary.

## AI Brain

Pixora не передаёт correction как голую строку. `app/edit_intent.py` строит сериализуемый EditPlan v2 с provider-neutral scene fields; `app/prompt_builder.py` создаёт только English/ASCII technical prompt. Correction редактирует private original выбранной успешной версии и меняет только затронутые поля, Repeat сохраняет тот же intent и input branch. Raw Russian text никогда не передаётся ImageProvider. Подробности: [аудит до изменений](docs/AI_BRAIN_AUDIT.md), [архитектура](docs/AI_BRAIN_ARCHITECTURE.md), [правила prompt](docs/PROMPT_ENGINEERING_RULES.md).

Безопасный административный просмотр intent/provider prompt:

```bash
python -m app.main ai-inspect --attempt-id <opaque-attempt-id>
```

Команда не выводит platform user ID, файловые пути, ключи или исходный пользовательский free text.

Личная AI-фотостудия: все работы пользователя, их версии, избранное и коллекции живут в одном месте. `photo-bot` — техническое имя, публичный бренд — Pixora. Продукт продаёт конкретную понравившуюся фотографию без watermark, а не модели, кредиты или абстрактные генерации. Реальный модерированный MAX-бот поддерживает закрытый owner-only режим: handlers включаются только при непустом `MAX_OWNER_USER_IDS`; остальные получают сообщение о закрытом тестировании без создания сессии и OpenAI-вызова. Без owner secret deploy принудительно сохраняет `MAX_POLL_OBSERVE_ONLY=true`.

## MAX production smoke

Безопасные административные команды:

```bash
python -m app.main max-check   # GET /me, без polling и сообщений
python -m app.main health      # SQLite + systemd + MainPID + fresh MAX contact + lock
```

Runtime запускается только как `/etc/systemd/system/photo-bot.service` под `photoapp`. Прямой background-start выведен из эксплуатации. Long Polling не считается публичным production transport; перед приглашением пользователей требуется Webhook HTTPS:443 и отдельный проверенный owner `/start` этап.

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

Одна demo-сессия закрепляется за одной исходной фотографией и допускает до `DEMO_MAX_SUCCESSFUL_GENERATIONS` успешно доставленных preview. Технические и policy-ошибки, а также ошибка доставки лимит не расходуют.

## Personal studio API

Каждая успешная попытка автоматически становится новой версией одной работы Gallery. `app/gallery.py` предоставляет операции для repeat/correction, before/after metadata, favorites/current best, collections, tags, preferences, search, recent/continue, trash и restore. Заблокированный original никогда не возвращается пользовательским DTO.

Новые работы используют `data/users/<opaque-user-id>/gallery/<item-id>`. Существующие demo-файлы не перемещаются: migration v2 создаёт ссылки на них и сохраняет обратную совместимость.

```powershell
python -m app.main gallery-cleanup           # dry-run
python -m app.main gallery-cleanup --execute # физическая очистка
```

Retention задают `DEMO_RETENTION_DAYS=30`, `PAID_RETENTION_DAYS=180` и `TRASH_RETENTION_DAYS=30`. Реальные MAX-экраны Gallery, before/after slider, интерактивный поиск и экспорт пока не реализованы.

## MAX closed-test transport

Официальный API contract и результаты поиска старого бота описаны в [MAX_TRANSPORT_AUDIT.md](docs/MAX_TRANSPORT_AUDIT.md). Реализованы owner allowlist, закрытый ответ для остальных и прямой UX `/start → фото → текст → результат`. Загрузка валидного source автоматически фиксирует согласие; отдельного Continue и prompt confirmation нет. Следующий текст после результата автоматически продолжает работу как correction. «✨ Идеи» — необязательный каталог. Processing, watermarked preview, correction/repeat, «Мои работы», favorite и физическое удаление сохранены.

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

Systemd hardening template находится в `ops/photo-bot.service`, но не устанавливается до получения MAX credentials и рабочего mode. Официальный production-вариант — Webhook; текущий VPS пока слушает только SSH.

## Документация

Главный источник истины — [docs/PROJECT_BIBLE.md](docs/PROJECT_BIBLE.md). Текущее состояние, архитектура, процесс поставки и решения описаны в остальных файлах каталога `docs/`.

### Hybrid processing

Pixora routes each request into a typed mode instead of using a generative provider
for every task. See [`PROCESSING_MODES_AUDIT.md`](docs/PROCESSING_MODES_AUDIT.md),
[`PROCESSING_MODES_ARCHITECTURE.md`](docs/PROCESSING_MODES_ARCHITECTURE.md),
[`SEGMENTATION_EVALUATION.md`](docs/SEGMENTATION_EVALUATION.md),
[`BACKGROUND_ASSET_POLICY.md`](docs/BACKGROUND_ASSET_POLICY.md) and
[`REAL_BACKGROUND_PIPELINE.md`](docs/REAL_BACKGROUND_PIPELINE.md).

```powershell
python scripts/check_asset_licenses.py
python scripts/benchmark_processing.py --iterations 3 --size 1024
```

Real-background mode stays disabled until licensed assets, pinned weights, VPS
resource measurements and owner-only visual QA are complete.
