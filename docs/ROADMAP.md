# Roadmap

## AI Brain

Выполнено: deterministic Russian intent parser, typed EditPlan, technical prompt builder, cumulative corrections, parent-original lineage, Repeat semantics, v4 migration, feedback и regression suite. Следующий шаг — один owner-only синтетический сценарий максимум из трёх image requests и сравнение наблюдаемого качества/latency с фактическим provider usage. До первых пользователей нужно накопить минимум 20 размеченных feedback cases и проверить долю исправлений, которые сохраняют identity/background continuity.

## Сайт Pixora — код готов, публикация ожидает инфраструктуру

Статический лендинг, SEO, legal placeholders, CI, Lighthouse и atomic release workflow готовы. До публикации нужны DNS/TLS/Nginx ownership, реальный MAX deep link, подтверждённая почта и юридически утверждённые документы. Следующее развитие перечислено в `site/docs/FUTURE_ROADMAP.md`.

## Этап 0 — production-фундамент

Hetzner, SSH hardening, UFW/fail2ban, отдельный deploy user, Python 3.12, CI/CD, healthcheck, secret scan и документация выполнены. Новый production OpenAI-ключ настроен; встроенный `openai-check` получил HTTP 200 и подтвердил доступность `gpt-image-2`.

## Этап 1 — технический image vertical slice

Выполнено: fake/OpenAI image-edit gateway, проверка сигнатуры и размера изображения, prompt/timeout limits, приватное filesystem storage, watermark preview, SQLite и CLI `demo-edit`/`demo-stats`. Один production smoke на синтетическом портрете подтвердил HTTP 200, latency около 33 секунд, сохранение личности и корректный watermark. Осталось накопить выборку и сверить provider usage с фактическим биллингом.

## Этап 2 — пользовательский канал (частично)

Реализованы проверенный официальный MAX API contract, thin HTTP transport, SQLite dialog state, legal versions, idempotency, single-instance polling, owner allowlist и полный автоматический fake vertical slice до Gallery/delete. Без owner secret production остаётся в transport-only observe mode. Следующий шаг — записать подтверждённый MAX user ID владельца в Environment secret и пройти live `/start`/upload/generation/history/repeat/correction E2E. До публичного запуска остаются production Webhook HTTPS:443, durable queue/worker и отказ от Long Polling.

## Этап 3 — операции и монетизация (частично)

SQLite, идемпотентность, technical non-debit, cost telemetry, correction flag и payment-intent/unlock guards реализованы. Остались измерение all-in себестоимости, реальный тестовый payment provider, подтверждение callback authenticity, цена и пользовательский платёжный UX.

## Этап 4 — закрытая beta

Базовые лимиты, delete, storage layout и cost/error telemetry реализованы. Остались retention cleanup, backup/restore SQLite, support, p95/quality dashboard, alerts, нагрузочная проверка и юридическая экспертиза.

## Этап 5 — масштабирование по данным

Оптимизировать конкурентность и ресурсы. Очередь, Redis, worker-процессы, объектное хранилище или новый сервер рассматриваются только после подтверждения bottleneck метриками.

## Этап 3.5 — личная AI-фотостудия (домен готов)

Реализованы Gallery, работы, версии, version-specific originals, favorites/current best, collections, tags, preferences, search, recent/continue, trash, retention cleanup и совместимый backfill. Следующий шаг — подключить эти операции к подтверждённому live MAX transport и платёжному UX, затем реализовать простые экраны Gallery.

Before/after slider, экспорт и расширенный визуальный поиск не входят в текущий этап. SQLite FTS, object storage и отдельные workers откладываются до подтверждения необходимости метриками. До beta также нужны расписание cleanup и backup/restore drill.

## Следующий контрольный шаг — live MAX smoke

Владелец подтверждает/создаёт тестового бота, безопасно добавляет `MAX_BOT_TOKEN` в GitHub Environment и сообщает username бота. После `max-check` временно запускается single-instance polling для одного синтетического сценария. Не более двух OpenAI edits, без оплаты, personal photo и original delivery. Затем polling останавливается; production включается только после Webhook/domain/TLS решения.
