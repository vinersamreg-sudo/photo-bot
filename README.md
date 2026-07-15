# photo-bot

Отдельный коммерческий ИИ-фоторедактор. `photo-bot` — техническое имя; публичный бренд пока не утверждён. Реализованы защищённая бесплатная demo-сессия, SQLite, image-edit gateway, watermark, приватное файловое хранение, CLI vertical slice, telemetry и транспорт-независимый MAX adapter. Реальный MAX transport и эквайринг ещё не подключены.

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

Процесс `run` пока является signal-aware каркасом без MAX polling/webhook, поэтому постоянно запускать его до появления подтверждённого transport handler не требуется.

## Документация

Главный источник истины — [docs/PROJECT_BIBLE.md](docs/PROJECT_BIBLE.md). Текущее состояние, архитектура, процесс поставки и решения описаны в остальных файлах каталога `docs/`.
