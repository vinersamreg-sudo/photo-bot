# Current State

## AI Brain — 16.07.2026

Реализован детерминированный слой интерпретации русских запросов: типизированный `EditPlan`, отрицания, накопление correction intent, отдельный English technical prompt и SQLite migration v4. Correction теперь использует private original выбранной успешной версии; Repeat сохраняет effective intent и input branch. В attempts/versions раздельно хранятся точный пользовательский текст, JSON plan, provider prompt, parent/source version и версии parser/builder.

Provider quality больше не зашит как `low`: production contract использует `IMAGE_EDIT_QUALITY=medium`, `IMAGE_EDIT_SIZE=1024x1024`, `IMAGE_EDIT_INPUT_FIDELITY=auto` (для `gpt-image-2` параметр не отправляется), PNG output, timeout 300 секунд и максимум 2 SDK retry. Сравнительные платные image requests ещё не выполнялись; live owner-only сценарий должен быть согласован после deploy.

Добавлены необязательные 👍/👎, агрегаты intent/correction/duration/version/provider status и admin-safe `ai-inspect`. 107 тестов подтверждают rocky-background regression, lineage, Repeat, delivery boundary, legacy migration и отсутствие provider вызова при противоречии.

## MAX owner-only transport smoke — 15.07.2026

Бот `Pixora обработка фото ИИ` создан владельцем и прошёл модерацию. `MAX_BOT_TOKEN` хранится в GitHub Environment `production` и доставляется в production `.env` только через stdin. Для текущего инфраструктурного этапа используется Long Polling в режиме `MAX_POLL_OBSERVE_ONLY=true`: соединение и marker живые, но пользовательские handlers, ответы, callback-обработка и OpenAI image generation отключены. Runtime управляется только `photo-bot.service` под `photoapp`; health требует свежую успешную связь с MAX, SQLite, MainPID и single-instance lock.

Owner-only application gate реализован fail-closed: handlers требуют `MAX_OWNER_USER_IDS`, посторонние получают вежливый закрытый ответ без диалога, файлов и генерации. Реальный пользовательский E2E остаётся отдельной production-проверкой; до публичного запуска обязателен Webhook HTTPS:443 с быстрой фиксацией события и отдельной фоновой обработкой.

## Сайт Pixora

В `site/` реализован отдельный responsive static landing: продуктовый Hero, сценарии, собственные синтетические «До/После», преимущества, четыре шага, FAQ, SEO metadata/schema, draft legal pages и MAX placeholder CTA. Site CI проверяет структуру, HTTP smoke и Lighthouse budgets ≥95. Production deploy подготовлен, но выключен до DNS/TLS/Nginx и подтверждённого MAX deep link.

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
- production smoke 15.07.2026 через CLI и реальный `gpt-image-2 images.edit` на синтетическом портрете получил HTTP 200 примерно за 33 секунды; создан 1024×1024 preview 134 KB с читаемым кириллическим watermark, original 1.59 MB пользователю не раскрыт, квота уменьшилась ровно с 5 до 4;
- после smoke production idle-процесс перезапущен на актуальном коде, log secret scan чистый.

## MAX vertical slice — реализовано в коде

- thin HTTP client официального `platform-api2.max.ru`: updates, messages, callbacks, media download/upload, edit/delete;
- нормализация `bot_started`, `message_created`, `message_callback` и стабильные event keys из `mid`/`callback_id`;
- SQLite migration v3: 12 состояний диалога, versioned legal acceptances, processed events, durable marker и transitions audit;
- восстановление `processing` после restart в `confirmation` без расхода quota;
- `/start`, legal gate, главное меню, «Своя идея», upload, prompt confirmation до OpenAI, processing status и preview-only delivery;
- correction/repeat той же GalleryItem, unlock placeholder, последние 10 работ, version navigation, favorite/current best;
- delete confirmation и немедленный физический purge файлов/путей;
- single-instance polling lock; `run` больше не поддерживает бессмысленный idle loop;
- установленный systemd runtime из hardening template `ops/photo-bot.service`.

## Не реализовано или не подтверждено live

Живое пользовательское сообщение/image/callback, production Webhook endpoint HTTPS:443, эквайринг/callback verification, платное продолжение, monitoring/alerting и backup/restore SQLite. Long Polling активен только для закрытого transport smoke и не считается публичным production transport.

## Известные ограничения

- официальный контракт и авторизация реального бота проверены, но пользовательский payload и delivery в клиенте MAX ещё не подтверждены;
- Environment `production` содержит `MAX_BOT_TOKEN`; значение не читается и доставляется на VPS через stdin, тестовый user пока не подтверждён;
- текущий VPS принимает только SSH; для Webhook нужны домен, TLS, 443 и firewall change;
- резервная стоимость smoke записана как 10 ₽ через `DEMO_ESTIMATED_COST_RUB_PER_GENERATION`; API не вернул готовую сумму в валюте, поэтому тарифы всё ещё требуют сверки token usage с фактическим счётом;
- payment confirmation пока является внутренней границей без реального провайдера и не должен вызываться из публичного transport;
- юридические тексты являются техническим draft и требуют отдельной экспертизы;
- Hetzner Cloud Firewall не настроен; внешний доступ сейчас ограничивает UFW.

## Проверенное окружение

Сервер отвечает из Германии, имеет доступ к `api.openai.com` по HTTPS, а запрос без ключа возвращает ожидаемый `401`. Публично слушает только SSH, failed systemd units отсутствуют, свободно около 35 GB. После системных обновлений выполнена контролируемая перезагрузка; key-based root access и состояние systemd повторно проверены.

## Personal studio — состояние 15.07.2026

Реализованы одна Gallery на пользователя, работы и версия-специфичные originals; автоматическая запись успешных demo-результатов и совместимый backfill migration v2; repeat/correction с parent/effective prompt; favorites/current best, rating, rename, collections, tags и явные preferences; поиск по title/scenario/tag/date/folder/favorite; recent/continue; soft delete/restore и `gallery-cleanup` с dry-run. Retention настраивается отдельно для demo (30 дней), paid (180 дней) и корзины.

Gallery API проверяет владельца и не раскрывает original заблокированной версии. MAX adapter содержит только контракт меню «Мои работы / Избранное / Последние / Коллекции / Корзина».

Минимальные fake-проверенные MAX views Gallery реализованы; реальный клиент MAX ещё не проверен. Не реализованы collections/tags/search UI, before/after slider, экспорт и scheduler cleanup. Поиск пока основан на SQLite `LIKE`, хранение — на приватном filesystem; backfill сохраняет legacy demo paths.
