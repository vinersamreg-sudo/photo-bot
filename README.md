# photo-bot

Личная AI-фотостудия: все работы пользователя, их версии, избранное и коллекции живут в одном месте. `photo-bot` — техническое имя; публичный бренд пока не утверждён. Продукт продаёт конкретную понравившуюся фотографию без watermark, а не модели, кредиты или абстрактные генерации. MAX transport реализован и fake-проверен, но live bot/token и эквайринг ещё не подключены.

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

Официальный API contract и результаты поиска старого бота описаны в [MAX_TRANSPORT_AUDIT.md](docs/MAX_TRANSPORT_AUDIT.md). Реализованы `/start`, versioned legal gate, меню, upload, prompt confirmation, processing, watermarked preview, correction/repeat, «Мои работы», favorite и физическое удаление.

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
