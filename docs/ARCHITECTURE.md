# Architecture

## Текущая схема

`GitHub main → GitHub Actions → SSH/rsync → /opt/photo-bot → Python 3.12 venv → OpenAI API`.

CI запускает unit-тесты, healthcheck, `pip check` и secret scan. Deploy выполняется непривилегированным `photoapp`, синхронизирует только код и сохраняет `.env`, `venv/`, `data/`, `logs/`, `temp/`. После проверки SHA фиксируется в `data/deployed_commit.txt`.

## Компоненты

- configuration: environment variables и `.env` через `app/config.py`;
- OpenAI boundary: создание клиента, классификация безопасных ошибок и проверка модели в `app/openai_client.py`;
- runtime CLI: health, OpenAI/MAX checks, `demo-edit`, `demo-stats` и MAX runtime selection в `app/main.py`;
- demo policy: quota, TTL, idempotency, concurrency, daily budgets и unlock guards в `app/demo_service.py`;
- persistence: SQLite schema/transactions/recovery в `app/database.py`;
- image boundary: OpenAI/fake providers в `app/image_provider.py`;
- private storage: `source/`, `originals/`, `previews/`, `metadata.json` в `app/storage.py`;
- preview protection: масштабируемый watermark в `app/watermark.py`;
- MAX HTTPS boundary: официальный API client/Update parser/media и polling lock в `app/max_transport.py`;
- MAX application: durable dialog orchestration в `app/max_application.py`, state/legal/idempotency в `app/max_conversation.py`, runtime composition в `app/max_runtime.py`;
- filesystem state: локальные каталоги `data`, `logs`, `temp`;
- operations: скрипты в `scripts/` и GitHub Actions.

## Demo workflow

`legal consent → source signature/size validation → user/session transaction → quota/budget/concurrency guard → idempotent attempt → OpenAI images.edit → private original → reduced watermark preview → delivery → single transactional quota increment`.

При network/provider/timeout/storage/policy/delivery failure попытка получает отдельный статус, но `successful_generations` не меняется. После перезапуска незавершённые `pending/processing` переводятся в `failed_technical`.

Файлы располагаются в `data/users/<opaque-user-id>/demo_sessions/<session-id>/{source,originals,previews}`. Platform ID, username или телефон не используются в путях. Original не входит в `DemoGenerationResult` и может быть получен только через paid payment intent.

Границы модулей: transport adapter, use-case/service, OpenAI image gateway, repository для операций/баланса, storage policy, observability. Внешние интеграции должны быть заменяемыми и покрываться тестами через fake-клиенты.

## Cost telemetry

Для каждой попытки сохраняются provider/model, requested size/quality/format, duration, status, request id, provider usage metadata, input/output bytes, retries, correction и technical-refund flags. Если provider не возвращает достаточных данных для точной стоимости, `estimated_cost` равен конфигурируемому budget reserve:

`estimated demo spend = successful attempts × DEMO_ESTIMATED_COST_RUB_PER_GENERATION`.

Это консервативный operational guard, а не бухгалтерская стоимость. После получения provider usage формула должна учитывать text input, high-fidelity image input и image output, а затем проверяться по фактическому счёту.

## Безопасность и эксплуатация

Секреты поступают только из окружения; логи редактируют известные значения ключей; пользовательский ввод и файлы имеют ограничения; операции получают UUID без персональных данных. Private directories/files закрыты от других POSIX-пользователей. Сервис не запускается от root. Systemd появится при реализации реального обработчика.

## Ограничения масштаба

2 GB RAM требуют ограничить размер изображений и число одновременных задач. Redis/Celery, отдельное хранилище и дополнительные узлы вводятся только после измерения очереди, памяти и времени обработки.

## Personal studio domain

`platform user → одна Gallery → gallery_item (source) → gallery_version (preview + version-specific original)`.

`app/gallery.py` отвечает за работы, версии, favorites, current best, collections, tags, preferences, recent, trash и retention. Успешная demo-попытка и её версия Gallery фиксируются в одной транзакции. Repeat использует effective prompt родителя; correction добавляет замечание и сохраняет parent link. Внешний DTO проверяет владельца и скрывает `original_path`, пока конкретная версия не разблокирована.

Migration v2 добавляет `galleries`, `gallery_items`, `gallery_versions`, `collections`, `tags`, `gallery_item_tags`, `user_preferences` и `schema_migrations`. Backfill существующих demo-данных не перемещает файлы. Новые независимые работы используют `data/users/<opaque-user-id>/gallery/<item-id>/{source,versions,metadata}`, а legacy demo layout остаётся читаемым.

Удаление имеет две стадии: soft delete задаёт `deleted_at`/`purge_after`; отдельный `gallery-cleanup` сначала показывает кандидатов, а с `--execute` удаляет приватное дерево и связанные строки. Поиск намеренно использует индексированные фильтры SQLite и `LIKE`; FTS и object storage отложены до измеримого объёма.

## MAX transport

`MAX Update → parse_update → processed-event guard → MaxApplication → MaxDemoAdapter → DemoService/GalleryService → MaxApiClient response`.

Transport не содержит quota, watermark, generation, payment или retention rules. `message.body.mid` дедуплицирует message, `callback.callback_id` — callback. Dialog state и Long Polling marker находятся в SQLite. Callback payload содержит только короткое действие и при необходимости opaque GalleryItem id.

API base URL — `platform-api2.max.ru`, token передаётся заголовком Authorization. Входной image скачивается по проверенному HTTPS media URL во временный файл, валидируется Pillow/domain storage и удаляется из temp. Preview получает upload token через `/uploads?type=image` и отправляется `/messages`; original не передаётся transport-слою для отправки.

Long Polling имеет advisory file lock и предназначен только для закрытой проверки. Целевая production topology: MAX Webhook → TLS termination на 443 → быстрый authenticated accept → durable SQLite event → application worker. Endpoint ещё не реализован, поскольку отсутствуют домен/TLS/secret и публичный порт; синхронно держать Webhook во время OpenAI edit нельзя из-за требования ответа MAX в пределах 30 секунд.
