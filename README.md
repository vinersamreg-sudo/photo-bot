# Лайви — коммерческий ИИ-фотобот

На текущем этапе репозиторий содержит только production-каркас: конфигурацию,
проверку файловой системы, проверку авторизации OpenAI API, безопасный процессный
каркас и автоматический deploy. MAX, обработка фото, оплата, баланс, очередь и
пользовательский интерфейс пока намеренно не реализованы.

## Требования

- Python 3.10.1 или новее из ветки 3.10+;
- Linux с `venv`, `pip`, `nohup`, `cron` и SSH для production;
- без Docker, Redis, Celery и Kubernetes.

## Локальная установка

Основная рабочая копия:

```text
C:\Users\viner\Documents\Codex\photo-bot
```

В PowerShell:

```powershell
cd C:\Users\viner\Documents\Codex\photo-bot
py -3.10 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Для локального `.env` замените `BASE_DIR` на абсолютный путь репозитория или
удалите значение: тогда корень определится автоматически. Реальный API-ключ
хранится только в `.env` и никогда не коммитится.

```dotenv
OPENAI_API_KEY=ваш_реальный_ключ
OPENAI_IMAGE_MODEL=gpt-image-2
APP_ENV=development
BASE_DIR=C:\Users\viner\Documents\Codex\photo-bot
```

## Проверки

```powershell
python -m unittest discover -v
python -m app.main health
python -m app.main openai-check
```

`health` проверяет версию Python, загрузку конфигурации, наличие и доступность
для записи каталогов `data`, `logs`, `temp`. `openai-check` требует ключ,
создаёт официальный Python-клиент и вызывает список моделей. Это подтверждает
авторизацию без генерации изображения. Любая ошибка возвращает ненулевой exit
code; секреты фильтруются из логов.

## Production

Корень приложения:

```text
/var/www/u3546857/data/apps/photo-bot
```

Python окружения:

```text
/var/www/u3546857/data/apps/photo-bot/venv/bin/python
```

Подготовьте `/var/www/u3546857/data/apps/photo-bot/.env` из `.env.example` и
укажите реальный ключ. Лог приложения: `logs/app.log`.

Запуск, проверка и остановка выполняются из любой текущей директории:

```bash
/var/www/u3546857/data/apps/photo-bot/scripts/start_bot.sh
/var/www/u3546857/data/apps/photo-bot/scripts/healthcheck.sh
/var/www/u3546857/data/apps/photo-bot/scripts/stop_bot.sh
```

Процесс хранит PID в `data/photo-bot.pid`. Скрипт остановки дополнительно
проверяет командную строку процесса и не посылает сигнал чужим Python-процессам.

## GitHub Actions deploy

Push в `main` запускает unit-тесты. Только после их успеха workflow копирует код
по SSH, не затрагивая `.env`, `venv/`, `data/`, `logs/`, `temp/`, устанавливает
зависимости через production `venv/bin/pip` и выполняет healthcheck. Workflow не
использует `sudo`, не перезапускает и не изменяет TripDay.

В GitHub repository settings добавьте secrets:

- `REG_RU_HOST` — SSH-хост REG.RU;
- `REG_RU_USER` — `u3546857`;
- `REG_RU_SSH_PRIVATE_KEY` — приватный SSH-ключ для deploy;
- `REG_RU_SSH_PORT` — SSH-порт хостинга.

Перед первым deploy на сервере должны существовать `.env`, `venv/`, `data/`,
`logs/`, `temp/`; публичная часть SSH-ключа должна быть добавлена в
`~/.ssh/authorized_keys`. Рекомендуется защитить GitHub environment
`production` правилами репозитория.
