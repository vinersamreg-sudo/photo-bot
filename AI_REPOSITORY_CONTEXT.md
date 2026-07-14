# AI repository context

## Назначение и текущая стадия

Это отдельный коммерческий проект ИИ-фотобота «Лайви». Сейчас реализован только
минимальный production-каркас: конфигурация, healthcheck, проверка авторизации
OpenAI, процессные скрипты, тесты и GitHub Actions deploy. Бизнес-логики обработки
фотографий ещё нет.

## Архитектура

- `app/config.py` — `.env`, настройки и production/local пути.
- `app/openai_client.py` — создание клиента и минимальная проверка API.
- `app/image_service.py` — пустая граница будущего сервиса изображений.
- `app/main.py` — CLI, healthcheck, логирование и long-running каркас.
- `scripts/` — PID-ориентированное управление только процессом photo-bot.
- `tests/` — unit-тесты без реальных OpenAI-запросов.

Архитектуру следует сохранять простой и совместимой с Python 3.10.1.

## Инфраструктурные ограничения

Production — обычный виртуальный хостинг REG.RU, не VPS. Доступны SSH, pip,
venv, nohup, cron, HTTPS к OpenAI и GitHub Actions deploy. Недоступны и запрещены
Docker, Redis, Celery и Kubernetes. `sudo` не используется.

## Пути

- Единственный источник изменений: `C:\Users\viner\Documents\Codex\photo-bot`.
- Production root: `/var/www/u3546857/data/apps/photo-bot`.
- Production Python: `/opt/python/python-3.10.1/bin/python`.
- Venv Python: `/var/www/u3546857/data/apps/photo-bot/venv/bin/python`.
- Log: `/var/www/u3546857/data/apps/photo-bot/logs/app.log`.
- PID: `/var/www/u3546857/data/apps/photo-bot/data/photo-bot.pid`.

Нельзя вручную править production как второй источник истины: изменения идут
из локального Git-репозитория через deploy.

## Изоляция от TripDay

Проект не связан с TripDay. Запрещено использовать или изменять его код,
процессы, директории, конфигурацию и deploy. Остановка процесса разрешена только
по проверенному PID `photo-bot`.

## Что пока запрещено добавлять

MAX-интеграцию, Telegram, оплату, SQLite-баланс, очередь, Redis, Celery, Docker,
Kubernetes, веб-панель и сложную архитектуру.

## Дальнейший целевой поток

`MAX → фотография → инструкция → OpenAI → результат`.

Каждый этап добавляется отдельно после стабилизации инфраструктурного каркаса.
