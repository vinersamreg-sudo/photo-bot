# Decisions

## ADR-041 — Permanent v1 package is 2 generations plus 1 selected original

Принято 20.07.2026 и заменяет ADR-035/036/038 в части состава продукта и момента выбора версии. Новый MAX user ID однократно получает две успешно доставленные генерации, глобальные для всех фотографий. Пакет `continuation_pack_2_plus_1` за 49 ₽ атомарно добавляет две generation credits и одно independent unlock entitlement. Покупка не привязывается к версии: пользователь позднее применяет entitlement к любой своей доступной GalleryVersion, созданной до или после покупки. Повторные пакеты складываются; подписки и auto-unlock нет. Reservation создаётся до provider и расходуется только после успешной доставки preview. Полностью неиспользованный пакет можно откатить атомарно; использованный пакет требует ручного возврата.

## ADR-040 — Moderation, sandbox, production payments and pilot are separate gates

Принято 20.07.2026. Публичная витрина может быть отправлена на модерацию Robokassa, но это не разрешает ResultURL, sandbox, реальные деньги или pilot handlers. ResultURL должен быть POST-only за HTTPS reverse proxy; sandbox evidence, fiscal settings, controlled owner payment, original delivery, refund и reconciliation проверяются отдельно. Опасные admin-команды по умолчанию dry-run и требуют `--apply`. `public_launch_ready` остаётся false независимо от других gate.

## ADR-039 — Site deploy изолирован и открывается только после HTTPS gate

Принято 19.07.2026. Официальный сайт публикуется из `site/public` в отдельные immutable releases `/opt/pixora-site/releases/<sha>` с atomic symlink и rollback. Nginx не имеет доступа к `/opt/photo-bot`. GitHub deploy остаётся выключенным, пока DNS, trusted TLS, внешний smoke и неизменность backend не доказаны.

## ADR-038 — Публичная цена равна 49 ₽ за одну выбранную версию (заменено ADR-041)

Принято 19.07.2026. Покупка относится только к одной выбранной `GalleryVersion` без водяного знака, не является подпиской и не разблокирует соседние версии. Пока платёжный контур не прошёл sandbox, модерацию и отдельное разрешение владельца, сайт не показывает кнопку оплаты и честно сообщает о закрытом тестировании.

## ADR-037 — Real payments are a separately approved rollout

Принято 19.07.2026. Commercial code does not authorize money movement. Deploy всегда принудительно ставит payments/provider/webhook/refunds в disabled/sandbox и `ROBOKASSA_PRODUCTION_APPROVED=false`. Production mode требует отдельного решения владельца после sandbox, HTTPS, legal/fiscal и support gates.

## ADR-036 — ResultURL commits payment before MAX delivery

Принято 19.07.2026. Валидный ResultURL атомарно подтверждает оплату и exact-version unlock. MAX delivery выполняется после commit; ошибка доставки переводит order в `delivery_pending`, но не отменяет оплату и не требует повторного платежа. Classic ResultURL не маскируется под ResultURL2: merchant/currency/expiry привязаны к local order, а provider timestamp отсутствует.

## ADR-035 — Покупается одна GalleryVersion

Принято 19.07.2026. PaymentOrder связывает user/attempt/version и разблокирует только `gallery_versions.id`. Нельзя разблокировать пользователя, всю Gallery, GalleryItem, sibling versions, future corrections или repeats. Refund and delivery history сохраняют эту же связь.

## ADR-034 — Pilot readiness requires restore-tested off-site backup

Принято 17.07.2026. Ежедневный SQLite snapshot шифруется и считается готовым только после фактической расшифровки, `quick_check` и внешней копии. Cleanup выполняется после off-site confirmation. `launch-status --strict` является автоматическим gate пилота, но не заменяет owner E2E.

## ADR-033 — OpenAI gpt-image-2 является единственным provider Pixora v1

Принято 17.07.2026. Production router выключен, real-background/segmentation experiments dormant. Не добавлять semantic parser, второй image engine или multi-provider platform до достаточной статистики пилота. Это решение заменяет ADR-027 и ADR-032/typed-hybrid утверждения в части production routing; их код и исследования остаются историческим материалом.

## ADR-032 — Закрытый запуск идёт ступенями owner / 5 / 10 / 20

Принято 17.07.2026. Pilot IDs хранятся как secret ordered allowlist, а активный prefix задаётся только 0/5/10/20. Обычный deploy возвращает observe-only и limit 0. Public access не включается этим механизмом.

## ADR-031 — Provider получает только нормализованный English contract

Принято 16.07.2026. `EditPlan` schema v2 содержит provider-neutral scene fields; prompt builder рендерит их на английском. Raw Russian user text хранится для audit/history, но не участвует в provider prompt. ImageProvider fail-fast отклоняет non-ASCII prompt до сетевого вызова. Это позволяет менять OpenAI на другой image provider без изменения MAX UX.

## ADR-030 — Основной MAX flow равен фото → текст → результат

Принято 16.07.2026. Для нового пользователя `/start` сразу открывает загрузку. Отдельное меню, «Своя идея», Continue и prompt confirmation удалены из основного пути. Согласие фиксируется только после успешной валидации и сохранения первой фотографии. Готовые сценарии остаются необязательным каталогом «Идеи». ADR-014 и ADR-018 в части UX заменены этим решением; таблицы legal/versioning и требования юридической экспертизы сохраняются.

## ADR-029 — Correction продолжает выбранную успешную версию

Принято 16.07.2026. Correction использует private original конкретной `parent_version_id`; отдельная `source_version_id` фиксирует фактический image input. Repeat сохраняет effective intent и повторяет тот же input branch. Новый initial/scenario edit использует immutable source работы. Parent без успешного доступного original отклоняется до provider-вызова.

## ADR-028 — Детерминированный EditPlan перед image provider

Принято 16.07.2026. Бытовой русский текст сначала преобразуется в сериализуемый EditPlan, затем объединяется с parent intent и только после этого становится техническим prompt. На первом этапе нет второго LLM/vision вызова. Отрицания имеют приоритет; schema v2 разрешает конфликт консервативно без дополнительного экрана. Legacy строки остаются читаемыми через migration v4.

## ADR-027 — Pixora является публичным брендом

Принято 15.07.2026 по прямому решению владельца. `photo-bot` остаётся техническим именем backend/repository, а сайт и пользовательский интерфейс используют Pixora или Pixora AI. ADR-005 и ADR-019 сохраняются как исторические решения, но в части статуса бренда заменены этим ADR. Продуктовая стратегия и позиционирование «платить за понравившийся результат, а не за модель» не меняются.

## ADR-001 — Hetzner VPS вместо REG.RU

Принято 14.07.2026. Production перенесён на Ubuntu VPS в Nuremberg, потому что исходящий адрес REG.RU получал от OpenAI региональный отказ. Proxy/VPN не используются. После успешного первого deploy старые REG.RU secrets удалены из GitHub.

## ADR-002 — Один простой Python-сервис

Для MVP выбран Python 3.12, venv и один процесс. Docker, Redis, Celery и Kubernetes отложены до появления измеримой нагрузки или требований к изоляции.

## ADR-003 — Разделение доступа

`vineradmin` используется для администрирования, `photoapp` — для приложения и GitHub deploy. У `photoapp` нет `sudo`; deploy ограничен `/opt/photo-bot`.

## ADR-004 — GitHub main как источник истины

Production не редактируется вручную. GitHub Actions выполняет тесты перед deploy, сохраняет runtime state и записывает SHA.

## ADR-005 — Публичный бренд не утверждён

До отдельного продуктового решения во внешних заявлениях используется нейтральное описание, в технике — `photo-bot`. Ранее обсуждавшиеся названия не считаются принятыми.

## ADR-006 — Systemd отложен

Текущий `run` — idle-каркас без пользовательской функции. Постоянный сервис и автозапуск добавляются вместе с реальным transport handler и его operational checks.

## ADR-007 — Host firewall как первый слой

UFW разрешает только SSH, fail2ban защищает `sshd`. Hetzner Cloud Firewall пока не добавлен из-за отсутствия настроенного API/консольного шага; это допустимо до появления публичного HTTP-порта.

## ADR-008 — Сценарное позиционирование вместо каталога моделей

Принято 14.07.2026 по результатам отдельного исследования публичной выдачи MAX. MVP объясняет задачи «создать портрет», «изменить фото», «восстановить фото», а модели остаются вторичным техническим атрибутом. Не копировать длинные SEO-названия и перегруженные меню конкурентов. Исследование не утверждает публичный бренд и не используется как доказательство выручки конкурентов.

## ADR-009 — Цена и правила до списания

Принято 15.07.2026 по результатам mystery shopping. До подтверждения операции показываются итоговая инструкция, цена, ожидаемое время и срок хранения. Бесплатный бонус не используется, если он не покрывает одну ключевую операцию. Подписка на канал не является обязательным шагом MVP.

## ADR-010 — Correction и автоматический технический возврат

Технический non-delivery возвращает баланс автоматически и допускает idempotent retry без второго списания. Визуально неудачный результат обрабатывается отдельным correction-flow по конкретному замечанию; случайная повторная версия не маскируется под исправление.

## ADR-011 — Сначала результат, затем оплата

Принято 15.07.2026. Бесплатный пользователь получает watermarked preview фактического результата и покупает разблокировку конкретного original, а не кредит или обещание генерации. Original доступен только при подтверждённом `paid` status.

## ADR-012 — Одна фотография и пять успешных demo-результатов

Один `(platform, platform_user_id)` получает одну demo-сессию, один неизменяемый source hash и максимум `DEMO_MAX_SUCCESSFUL_GENERATIONS` успешно доставленных preview. Начальное значение — 5: его достаточно для первого результата, correction и нескольких вариантов, но расходы остаются жёстко ограничены.

## ADR-013 — Delivery определяет расход квоты

Счётчик увеличивается только после сохранения original, создания watermark preview и успешной доставки preview. Network/provider/timeout/storage/internal/policy/delivery failures квоту не расходуют. Idempotency key не позволяет повторному MAX event увеличить счётчик второй раз.

## ADR-014 — «Своя идея» первая, модели скрыты

Историческое решение, заменено ADR-030. Названия моделей по-прежнему скрыты, но основного меню больше нет.

## ADR-015 — Серверный watermark и раздельное хранение

Бесплатный результат уменьшается до `DEMO_MAX_DIMENSION` и получает повторяющийся диагональный watermark «ОБРАЗЕЦ» плюс технический demo-id. Source, original и preview хранятся раздельно под непрозрачными UUID в каталогах с закрытыми правами.

## ADR-016 — Двухуровневое ограничение бесплатных расходов

Помимо пользовательского cooldown/attempt/concurrency limit действуют process-wide semaphore, дневной generation limit и дневной cost reserve. Cost reserve — конфигурируемая плановая оценка, не утверждение о точном счёте провайдера.

## ADR-017 — SQLite и восстановление после перезапуска

Для одного process model выбран SQLite. Незавершённые `pending/processing` попытки при старте переводятся в `failed_technical` без расхода demo-квоты. Redis, Celery и внешняя очередь не вводятся.

## ADR-018 — Юридическое подтверждение до upload

Историческое решение, заменено ADR-030 в части UX. Согласие фиксируется действием загрузки после успешной валидации файла; юридические тексты и достаточность такого механизма требуют экспертизы перед коммерческим запуском.

## ADR-019 — Публичный бренд остаётся не утверждённым

Ни одно ранее обсуждавшееся название не считается принятым. В коде, deploy и документации используется техническое имя `photo-bot`.

## ADR-020 — Личная AI-фотостудия вместо счётчика генераций

Принято 15.07.2026. Продукт хранит личную историю красивых фотографий и продаёт разблокировку конкретной версии. Названия моделей, кредиты и абстрактное количество генераций не являются основной коммерческой сущностью.

## ADR-021 — Одна Gallery, работы и неизменяемые версии

У каждого пользователя ровно одна Gallery. `gallery_item` представляет работу с одним source, а каждый успешный результат — отдельную `gallery_version` со своим original. Repeat и correction добавляют версию к той же работе; correction наследует effective prompt родительской версии и добавляет замечание. Favorite версии и current best независимы.

## ADR-022 — Совместимая миграция хранения

Схема migration v2 создаёт Gallery для существующих пользователей, связывает существующие demo-сессии и attempts с работами и версиями, не перемещая production-файлы. Для новых независимых работ канонический путь — `users/<opaque-user-id>/gallery/<item-id>/{source,versions,metadata}`. Старый demo layout остаётся читаемым до отдельной измеримой необходимости миграции файлов.

## ADR-023 — Soft delete и управляемый retention

Удаление сначала переносит работу в корзину. Физическая очистка выполняется отдельной идемпотентной задачей `gallery-cleanup`, которая по умолчанию работает в dry-run. Сроки задаются `DEMO_RETENTION_DAYS` (30), `PAID_RETENTION_DAYS` (180) и `TRASH_RETENTION_DAYS` (30).

## ADR-024 — Только явные preferences

Настройки пользователя сохраняются только по явному действию и применяются для интерфейса и удобства продолжения работы. Они не являются скрытым обучающим датасетом и не дают права использовать пользовательские изображения для обучения.

## ADR-025 — Масштабирование после метрик

На текущем объёме поиск использует индексированные поля SQLite и `LIKE`, файлы остаются в приватном filesystem. FTS, объектное хранилище, очередь и отдельные workers вводятся только после подтверждённых bottleneck-метрик, а не заранее.

## ADR-026 — MAX transport внутри единого Python-приложения

Принято 15.07.2026 после аудита `docs/MAX_TRANSPORT_AUDIT.md`. Отдельного существующего MAX-проекта не найдено, поэтому transport добавляется тонким модулем в `photo-bot` и вызывает существующие application/domain services. Отдельный HTTP-микросервис не создаётся. Прямой клиент официального HTTPS API предпочтён обязательной зависимости от Python SDK, который сам обозначен как неофициальный форк, проверенный командой MAX.

Production-механизм — Webhook с HTTPS:443 и проверкой `X-Max-Bot-Api-Secret`. Long Polling разрешён только как single-instance режим для разработки и закрытого live smoke и не считается production-ready. До появления MAX token, bot identity, домена/TLS и webhook secret запрещено утверждать, что live MAX integration работает.

## ADR-027 — Typed hybrid processing и fail-closed real backgrounds

Принято 16.07.2026. Generative provider больше не является единственным backend.
Router выбирает `AI_GENERATION`, `REAL_BACKGROUND_COMPOSITE`, `LOCAL_AI_EDIT`,
`ENHANCEMENT` или `RESTORATION`; `ProcessingPlan` сохраняется в attempt/version.

Реальный фон разрешён только из локального каталога с commercial license record и
SHA-256. Нет asset/model/mask — нет обработки. Silent fallback в AI запрещён.
Опциональный rembg CPU с pre-provisioned `u2net_human_seg` и pinned checksum
остаётся disabled до VPS resource benchmark и owner visual approval. AI finishing
также disabled.

## ADR-028 — OpenAI memory is auxiliary and fail-open

Принято 19.07.2026. Pixora SQLite, private Storage, GalleryVersion lineage и
SceneIntent остаются источником истины. Для контролируемого эксперимента добавлен
Responses image-tool adapter с `gpt-image-2` и `previous_response_id` конкретной
parent version. Одна remote Conversation на всё дерево отклонена: она линейна и
может смешать независимые ветви.

Repeat остаётся stateless. Missing/invalid/expired context выполняет stateless
fallback. Policy и exhausted quota не создают второй вызов. Feature flags по
умолчанию выключены, deploy возвращает их в `false`. Решение о пилоте отложено до
owner-only сравнения качества, latency и стоимости.
