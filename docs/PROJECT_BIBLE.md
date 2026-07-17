# Pixora Project Bible

Актуально с 17.07.2026. Этот документ имеет приоритет над историческими ADR и описаниями hybrid-processing.

## Продукт

Pixora — AI-редактор фотографий в MAX. `photo-bot` — только техническое имя репозитория и systemd unit. Продукт продаёт понравившийся результат, а не модель, токены или подписку.

Основной v1-путь: `/start → фотография → текст → обработка → демо с watermark → исправить / другой вариант / Мои работы`. До первой обработки нет меню сценариев и нет подтверждения запроса. «Идеи» — необязательный вторичный каталог.

## Зафиксированный v1 scope

- один production image provider: OpenAI `gpt-image-2` через `/v1/images/edits`;
- quality `medium`, output `1024x1024` PNG, timeout 300 секунд, до двух SDK retries;
- структурированный SceneIntent/EditPlan и English technical prompt;
- correction от выбранной успешной версии, repeat в той же ветке;
- preview с watermark, Gallery/versions/favorite/delete, placeholder оригинала;
- SQLite, private filesystem, один systemd process, MAX polling;
- owner allowlist и закрытые этапы 5/10/20 пользователей;
- privacy-minimal telemetry, backup/restore, cleanup и `launch-status`.

Экспериментальные hybrid/real-background/segmentation модули остаются в коде, но `PROCESSING_MODE_ROUTER_ENABLED=false`; они не являются production-путём v1. Semantic parser, второй provider, платный фотобанк, очередь, Redis, mini app и публичный запуск отложены до данных пилота.

## Безопасность и данные

Секреты хранятся только в GitHub Environment и `/opt/photo-bot/.env` с mode `600`; их нельзя выводить, коммитить или передавать в аргументах. Original не отправляется до подтверждённой оплаты. Технические, provider, storage и delivery ошибки квоту не списывают.

Телеметрия хранит тип события, технические связи, duration/cost/error/fallback. Она не хранит отдельную копию prompt, изображения, MAX ID или биометрию.

SQLite ежедневно копируется online-backup API, шифруется AES-256-CBC/PBKDF2, проходит реальное восстановление и копируется в GitHub Actions artifact. Cleanup запускается только после подтверждения off-site copy. Retention: backup 14 дней, demo 30, paid 180, trash 30.

## Доступ и запуск

Production: Hetzner Ubuntu, `/opt/photo-bot`, user `photoapp`, `photo-bot.service`. Обычный deploy всегда оставляет `MAX_POLL_OBSERVE_ONLY=true`. Handlers включаются только ручным workflow и только при owner secret. Pilot users берутся из секретного ordered allowlist, активный prefix — строго 0/5/10/20.

Публичная готовность запрещено заявлять до реального owner E2E, успешного backup/restore/off-site run, cleanup, monitoring и юридической проверки. После owner E2E observe-only возвращается, если владелец явно не разрешил иное.

## Ограничения качества

`gpt-image-2` недетерминирован: лицо и незапрошенные детали могут измениться, локальная правка может затронуть фон, последовательные edits могут накапливать drift. Pixora не обещает pixel-perfect Photoshop. Решение о втором provider принимается только после достаточной статистики реальных пользователей.

## Definition of done

Tests/secret scan/pip check/health зелёные; backup реально восстановлен; orphan cleanup проверен; `launch-status` не раскрывает секреты; commit, Actions run и production SHA совпадают; реальные image requests заранее согласованы и посчитаны; документация разделяет «реализовано» и «подтверждено live».
