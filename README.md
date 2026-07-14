# photo-bot

Отдельный коммерческий ИИ-фотобот. `photo-bot` — техническое имя; публичный бренд пока не утверждён. Сейчас репозиторий содержит production-каркас: конфигурацию, healthcheck, безопасную проверку OpenAI API, процессные скрипты, тесты и автоматический деплой. Пользовательский бот, обработка фотографий, платежи и очередь ещё не реализованы.

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

Процесс `run` пока является пустым signal-aware каркасом, поэтому постоянно запускать его в production до появления реального обработчика не требуется.

## Документация

Главный источник истины — [docs/PROJECT_BIBLE.md](docs/PROJECT_BIBLE.md). Текущее состояние, архитектура, процесс поставки и решения описаны в остальных файлах каталога `docs/`.
