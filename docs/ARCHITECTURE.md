# Architecture

## Текущая схема

`GitHub main → GitHub Actions → SSH/rsync → /opt/photo-bot → Python 3.12 venv → OpenAI API`.

CI запускает unit-тесты, healthcheck, `pip check` и secret scan. Deploy выполняется непривилегированным `photoapp`, синхронизирует только код и сохраняет `.env`, `venv/`, `data/`, `logs/`, `temp/`. После проверки SHA фиксируется в `data/deployed_commit.txt`.

## Компоненты

- configuration: environment variables и `.env` через `app/config.py`;
- OpenAI boundary: создание клиента, классификация безопасных ошибок и проверка модели в `app/openai_client.py`;
- runtime CLI: health, API-check, `demo-edit`, `demo-stats` и idle run-loop в `app/main.py`;
- demo policy: quota, TTL, idempotency, concurrency, daily budgets и unlock guards в `app/demo_service.py`;
- persistence: SQLite schema/transactions/recovery в `app/database.py`;
- image boundary: OpenAI/fake providers в `app/image_provider.py`;
- private storage: `source/`, `originals/`, `previews/`, `metadata.json` в `app/storage.py`;
- preview protection: масштабируемый watermark в `app/watermark.py`;
- MAX UX boundary: меню, consent gate и action mapping в `app/max_adapter.py`, без неподтверждённого сетевого transport;
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
