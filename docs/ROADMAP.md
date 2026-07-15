# Roadmap

## Этап 0 — production-фундамент

Hetzner, SSH hardening, UFW/fail2ban, отдельный deploy user, Python 3.12, CI/CD, healthcheck, secret scan и документация выполнены. Новый production OpenAI-ключ настроен; встроенный `openai-check` получил HTTP 200 и подтвердил доступность `gpt-image-2`.

## Этап 1 — технический image vertical slice

Выполнено: fake/OpenAI image-edit gateway, проверка сигнатуры и размера изображения, prompt/timeout limits, приватное filesystem storage, watermark preview, SQLite и CLI `demo-edit`/`demo-stats`. Один production smoke на синтетическом портрете подтвердил HTTP 200, latency около 33 секунд, сохранение личности и корректный watermark. Осталось накопить выборку и сверить provider usage с фактическим биллингом.

## Этап 2 — пользовательский канал (частично)

Реализованы проверенный официальный MAX API contract, thin HTTP transport, SQLite dialog state, legal versions, idempotency, single-instance polling smoke runner и полный fake vertical slice до Gallery/delete. Остались `MAX_BOT_TOKEN`, подтверждение реального bot/user, живой smoke и production Webhook HTTPS:443. Systemd template готов, но не установлен до появления рабочего transport mode.

## Этап 3 — операции и монетизация (частично)

SQLite, идемпотентность, technical non-debit, cost telemetry, correction flag и payment-intent/unlock guards реализованы. Остались измерение all-in себестоимости, реальный тестовый payment provider, подтверждение callback authenticity, цена и пользовательский платёжный UX.

## Этап 4 — закрытая beta

Базовые лимиты, delete, storage layout и cost/error telemetry реализованы. Остались retention cleanup, backup/restore SQLite, support, p95/quality dashboard, alerts, нагрузочная проверка, юридическая экспертиза и утверждение публичного бренда.

## Этап 5 — масштабирование по данным

Оптимизировать конкурентность и ресурсы. Очередь, Redis, worker-процессы, объектное хранилище или новый сервер рассматриваются только после подтверждения bottleneck метриками.

## Этап 3.5 — личная AI-фотостудия (домен готов)

Реализованы Gallery, работы, версии, version-specific originals, favorites/current best, collections, tags, preferences, search, recent/continue, trash, retention cleanup и совместимый backfill. Следующий шаг — подключить эти операции к подтверждённому live MAX transport и платёжному UX, затем реализовать простые экраны Gallery.

Before/after slider, экспорт и расширенный визуальный поиск не входят в текущий этап. SQLite FTS, object storage и отдельные workers откладываются до подтверждения необходимости метриками. До beta также нужны расписание cleanup и backup/restore drill.

## Следующий контрольный шаг — live MAX smoke

Владелец подтверждает/создаёт тестового бота, безопасно добавляет `MAX_BOT_TOKEN` в GitHub Environment и сообщает username бота. После `max-check` временно запускается single-instance polling для одного синтетического сценария. Не более двух OpenAI edits, без оплаты, personal photo и original delivery. Затем polling останавливается; production включается только после Webhook/domain/TLS решения.
