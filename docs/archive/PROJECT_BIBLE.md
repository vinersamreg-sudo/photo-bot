# Ravuna Project Bible

Актуально с 17.07.2026. Этот документ имеет приоритет над историческими ADR и описаниями hybrid-processing.

## Продукт

Ravuna — AI-редактор фотографий в MAX. `photo-bot` — только техническое имя репозитория и systemd unit. Продукт продаёт цифровой продукт «Пакет доступа Ravuna», а не модель, токены, генерации или подписку.

Основной v1-путь: `/start → фотография → текст → обработка → демо с watermark → исправить / другой вариант / Мои работы`. До первой обработки нет меню сценариев и нет подтверждения запроса. «Идеи» — необязательный вторичный каталог.

## Постоянная коммерческая модель Ravuna v1

Новый MAX user ID один раз получает две успешно доставленные обработки с watermark; баланс глобален для всех его фотографий. Ошибка provider/network/policy/storage/MAX, отмена или duplicate не расходуют его. Публичный цифровой продукт «Пакет доступа Ravuna» стоит 49 ₽. После подтверждения оплаты пакет атомарно добавляет две обработки и одно независимое право на original. Версию пользователь выбирает сам до или после расходования обработок; допускается любая существующая собственная версия до или после покупки. Внутренний код остаётся `continuation_pack_2_plus_1`, а credit/entitlement ledger не меняется. Пакеты складываются, подписки и автоматической разблокировки нет.

## Зафиксированный v1 scope

- один production image provider: OpenAI `gpt-image-2` через `/v1/images/edits`;
- quality `medium`, output `1024x1024` PNG, timeout 300 секунд, до двух SDK retries;
- структурированный SceneIntent/EditPlan и English technical prompt;
- correction от выбранной успешной версии, repeat в той же ветке;
- preview с watermark, Gallery/versions/favorite/trash/restore и exact-version original unlock;
- SQLite, private filesystem, один systemd process, MAX polling;
- owner allowlist и закрытые этапы 5/10/20 пользователей;
- privacy-minimal telemetry, backup/restore, cleanup и `launch-status`.

Экспериментальные hybrid/real-background/segmentation модули остаются в коде, но `PROCESSING_MODE_ROUTER_ENABLED=false`; они не являются production-путём v1. Semantic parser, второй provider, платный фотобанк, очередь, Redis, mini app и публичный запуск отложены до данных пилота.

## Безопасность и данные

Секреты хранятся только в GitHub Environment и `/opt/photo-bot/.env` с mode `600`; их нельзя выводить, коммитить или передавать в аргументах. Original не отправляется до подтверждённой оплаты. Технические, provider, storage и delivery ошибки квоту не списывают.

Телеметрия хранит тип события, технические связи, duration/cost/error/fallback. Она не хранит отдельную копию prompt, изображения, MAX ID или биометрию.

SQLite ежедневно копируется online-backup API, шифруется AES-256-CBC/PBKDF2, проходит реальное восстановление и копируется в GitHub Actions artifact. Cleanup запускается только после подтверждения off-site copy. Retention: backup 14 дней, demo 30, paid 180, trash 30.

Migration v8 сохраняет Robokassa order/event/webhook/receipt/audit/refund boundary. Migration v9 добавляет глобальный credit ledger, reservation до provider, consume только после доставки, release при любом сбое, атомарный package grant и отдельно расходуемые original entitlements. Реальные платежи не являются включённой функцией: deploy принудительно оставляет payments/webhook/refunds off, provider disabled, sandbox и production approval false. Первый настоящий платёж требует отдельного разрешения и выполнения `COMMERCIAL_LAUNCH.md`.

## Доступ и запуск

Production: Hetzner Ubuntu, `/opt/photo-bot`, user `photoapp`, `photo-bot.service`. Обычный deploy всегда оставляет `MAX_POLL_OBSERVE_ONLY=true`. Handlers включаются только ручным workflow и только при owner secret. Pilot users берутся из секретного ordered allowlist, активный prefix — строго 0/5/10/20.

Публичная готовность запрещено заявлять до реального owner E2E, успешного backup/restore/off-site run, cleanup, monitoring и юридической проверки. После owner E2E observe-only возвращается, если владелец явно не разрешил иное.

## Ограничения качества

`gpt-image-2` недетерминирован: лицо и незапрошенные детали могут измениться, локальная правка может затронуть фон, последовательные edits могут накапливать drift. Ravuna не обещает pixel-perfect Photoshop. Решение о втором provider принимается только после достаточной статистики реальных пользователей.

## Definition of done

Tests/secret scan/pip check/health зелёные; backup реально восстановлен; orphan cleanup проверен; `launch-status` не раскрывает секреты; commit, Actions run и production SHA совпадают; реальные image requests заранее согласованы и посчитаны; документация разделяет «реализовано» и «подтверждено live».

## Optional provider memory

Ravuna хранит память работы сама, а OpenAI conversation используется как
дополнительный контекст для последовательных правок. Migration v7 и Responses
adapter не меняют production v1: все flags выключены и основной endpoint —
`/v1/images/edits`. Включение возможно только после отдельного owner approval и
контролируемого сравнения; identity и visual drift не считаются решёнными.

## Content Studio

Ravuna Content Studio — отдельная операторская подсистема официального MAX-канала,
а не часть пользовательского photo flow. Её источник — только специально созданные
для Ravuna материалы с подтверждённым коммерческим разрешением. Она хранит
DemoAsset/Transformation/Result/Post в отдельной SQLite, создаёт Pillow-карточки и
фактический русский copy по шаблонам, затем требует ручной review. Истории клиентов,
отзывы и заказы не выдумываются; disclosure обязателен. Реальная публикация,
автопубликация и OpenAI-вызовы выключены. До импорта реального контента обязателен
отдельный restore-tested backup Content Studio DB и файлов.
