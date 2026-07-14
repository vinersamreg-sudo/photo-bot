# Current State

Состояние на 14.07.2026.

## Реализовано

- минимальный Python-каркас и `.env`-конфигурация;
- filesystem healthcheck для `data`, `logs`, `temp`;
- OpenAI client check без генерации: авторизация и видимость `gpt-image-2`;
- безопасная классификация API-ошибок и редактирование ключа в логах;
- signal-aware idle run-loop и PID-скрипты;
- unit-тесты и проверка deploy policy;
- secret scan отслеживаемых Git-файлов;
- Hetzner VPS, пользователи `vineradmin`/`photoapp`, venv Python 3.12;
- SSH hardening, UFW и fail2ban;
- GitHub Environment `production` с Hetzner deploy secrets;
- автоматический rsync deploy в `/opt/photo-bot`.

## Не реализовано

Интеграция с мессенджером, приём/обработка фото, вызов image generation/edit endpoint, UI, база операций, платежи, баланс, очередь, systemd-сервис, мониторинг и backup данных.

## Известные блокеры

- новый безопасный `OPENAI_API_KEY` ещё не добавлен; раскрытый ключ не используется;
- поэтому реальная проверка OpenAI из CI и Hetzner пропускается до настройки секрета;
- Hetzner Cloud Firewall не настроен; внешний доступ сейчас ограничивает UFW.

## Проверенное окружение

Сервер отвечает из Германии, имеет доступ к `api.openai.com` по HTTPS, а запрос без ключа возвращает ожидаемый `401`. Публично слушает только SSH, failed systemd units отсутствуют, свободно около 35 GB. После системных обновлений выполнена контролируемая перезагрузка; key-based root access и состояние systemd повторно проверены.
