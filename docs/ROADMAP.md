# Roadmap

## Этап 0 — production-фундамент

Hetzner, SSH hardening, UFW/fail2ban, отдельный deploy user, Python 3.12, CI/CD, healthcheck, secret scan и документация выполнены. Новый production OpenAI-ключ настроен; встроенный `openai-check` получил HTTP 200 и подтвердил доступность `gpt-image-2`.

## Этап 1 — технический image vertical slice

Выполнено: fake/OpenAI image-edit gateway, проверка сигнатуры и размера изображения, prompt/timeout limits, приватное filesystem storage, watermark preview, SQLite и CLI `demo-edit`/`demo-stats`. Один production smoke на синтетическом портрете подтвердил HTTP 200, latency около 33 секунд, сохранение личности и корректный watermark. Осталось накопить выборку и сверить provider usage с фактическим биллингом.

## Этап 2 — пользовательский канал (частично)

Реализован транспорт-независимый MAX adapter: согласия, «Своя идея» первой, компактное меню, каталог сценариев, upload одного source, demo preview, remaining count, correction/repeat/delete и unlock placeholder. Остались подтверждённый MAX API/event contract, live transport, сохранение состояния диалога, systemd, restart policy и operational check реального handler.

## Этап 3 — операции и монетизация (частично)

SQLite, идемпотентность, technical non-debit, cost telemetry, correction flag и payment-intent/unlock guards реализованы. Остались измерение all-in себестоимости, реальный тестовый payment provider, подтверждение callback authenticity, цена и пользовательский платёжный UX.

## Этап 4 — закрытая beta

Базовые лимиты, delete, storage layout и cost/error telemetry реализованы. Остались retention cleanup, backup/restore SQLite, support, p95/quality dashboard, alerts, нагрузочная проверка, юридическая экспертиза и утверждение публичного бренда.

## Этап 5 — масштабирование по данным

Оптимизировать конкурентность и ресурсы. Очередь, Redis, worker-процессы, объектное хранилище или новый сервер рассматриваются только после подтверждения bottleneck метриками.
