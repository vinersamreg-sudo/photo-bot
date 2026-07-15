# Current State

Состояние на 15.07.2026.

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
- автоматический rsync deploy в `/opt/photo-bot`;
- первый deploy коммита `d80f7e0` успешно прошёл через GitHub Actions;
- устаревшие REG.RU repository secrets удалены после подтверждения миграции.
- production-процесс `photo-bot` запущен; встроенный OpenAI check прочитал ключ из `/opt/photo-bot/.env`, получил HTTP 200 и подтвердил модель `gpt-image-2`;
- проведено отдельное публичное исследование MAX; продуктовые выводы перенесены в стратегию без смешивания исходников и данных проектов.
- завершён безопасный mystery shopping восьми выбранных конкурентов MAX: подтверждены реальные onboarding/paywall, цены, четыре бесплатных результата, correction и юридические паттерны; сырьё и screenshots остались в отдельном проекте;
- сформирован продуктовый blueprint с минимальным сценарием, pricing-формулами и политиками retry/correction/refund/storage/delete.
- реализованы SQLite-таблицы `users`, `demo_sessions`, `generation_attempts`, `payment_intents`, `legal_consents`;
- одна demo-сессия закрепляется за одним platform user и одним source SHA-256 на 60 минут;
- максимум успешных результатов конфигурируется через `DEMO_MAX_SUCCESSFUL_GENERATIONS`, начальное значение 5;
- квота увеличивается только после сохранения original, создания watermark preview и успешной доставки;
- добавлены idempotency key, per-user concurrency/cooldown/hourly limit, process semaphore и дневные generation/cost limits;
- source/original/preview раздельно хранятся под непрозрачными UUID; приватные каталоги имеют режим `0700`, файлы `0600` на POSIX;
- реализован масштабируемый кириллический watermark «ОБРАЗЕЦ», preview до 1024 px, JPEG quality 82;
- добавлены OpenAI/fake image-edit providers, CLI `demo-edit`, административный `demo-stats` и cost/latency/error telemetry;
- подготовлен idempotent `unlock_original`: pending intent не раскрывает original, paid status открывает конкретный файл;
- реализован транспорт-независимый MAX adapter с legal gate, меню, каталогом, correction/repeat/delete/unlock actions.

## Не реализовано

Реальный MAX polling/webhook transport, production event handler, эквайринг/callback verification, платное продолжение, retention cleanup, systemd unit, мониторинг, alerting и backup/restore SQLite.

## Известные ограничения

- MAX UX/domain adapter реализован, но сетевой transport требует подтверждённого API/event contract и credentials;
- резервная стоимость `DEMO_ESTIMATED_COST_RUB_PER_GENERATION` нужна только для budget guard; тарифы требуют измерения фактической all-in себестоимости;
- payment confirmation пока является внутренней границей без реального провайдера и не должен вызываться из публичного transport;
- юридические тексты являются техническим draft и требуют отдельной экспертизы;
- Hetzner Cloud Firewall не настроен; внешний доступ сейчас ограничивает UFW.

## Проверенное окружение

Сервер отвечает из Германии, имеет доступ к `api.openai.com` по HTTPS, а запрос без ключа возвращает ожидаемый `401`. Публично слушает только SSH, failed systemd units отсутствуют, свободно около 35 GB. После системных обновлений выполнена контролируемая перезагрузка; key-based root access и состояние systemd повторно проверены.
