# Pixora v1 — отчёт о внутренней модели 2+1

Дата: 20.07.2026. Статус этого документа до post-deploy разделён на «реализовано и локально проверено» и «production evidence»; отсутствие live evidence не заменяется предположением.

## Модель

1. До изменений demo quota находилась в сессии/фотографии, успешный платёж был привязан к одной версии и сразу разблокировал её.
2. Новый MAX user ID теперь получает ровно две обработки один раз за всю жизнь identity; в коде они по-прежнему учитываются как generation credits.
3. Credit резервируется перед provider и расходуется только после успешной доставки preview.
4. Баланс общий для разных фотографий и GalleryItems.
5. Публичный цифровой продукт «Пакет доступа Pixora» стоит 49 ₽; внутренний `continuation_pack_2_plus_1` начисляет +2 generation credits и +1 unlock entitlement.
6. Два credit начисляются одним package grant и одним source lot.
7. Entitlement создаётся в той же SQLite transaction, что credits и подтверждение callback.
8. Original выбирается пользователем отдельно, сразу или позже.
   Право сначала резервируется, а расходуется и связывается с версией только после
   успешной доставки оригинала через MAX; при сбое доставки резерв возвращается.
9. Entitlement применим к собственной доступной версии до или после покупки.
10. Повторные покупки складываются; replay одного callback не складывается.
11. Correction создаёт новую версию от выбранного parent и расходует один credit после доставки.
12. Repeat создаёт новый вариант в lineage и учитывается так же.
13. Новая фотография не выдаёт новые free credits и может использовать остаток общего баланса.
14. Provider/network/timeout/policy/storage/internal/MAX delivery/cancel/duplicate не расходуют credit.
15. Reservation и idempotency key не позволяют двум параллельным запросам использовать один credit.
16. Restart recovery возвращает stale generation reservation только когда attempt
    больше не pending/processing и отдельно освобождает незавершённый резерв доставки
    original, не затрагивая refund hold.
17. Полностью неиспользованный пакет допускает scoped rollback; любая использованная часть требует manual review.
18. Abuse ограничивается lifetime grant по MAX ID, cooldown/hourly/daily/concurrency/budget checks, non-negative constraints, callback replay protection и audited admin adjustment.

## Публичный и платёжный контракт

19. Оферта описывает две бесплатные успешные обработки, цифровой продукт «Пакет доступа Pixora» за 49 ₽, отдельный выбор original и правила возврата.
20. Главная, FAQ и structured data обещают две обработки и один оригинал, не продают отдельные генерации и не обещают подписку.
21. Письменный ответ Robokassa для «Робочеков СМЗ» закрепляет один Receipt item «Пакет доступа Pixora», quantity 1, sum 49.00, tax none; `sno`, `payment_method` и `payment_object` отсутствуют. Использование пакета новых чеков не создаёт.
22. Экономика пересчитана по фактически использованным, а не выданным credits; выполненный notebook хранит параметры и сценарии.

## Migration v9

23. Migration v9 создаёт account/lot/reservation/ledger/package-grant/entitlement/admin-audit state и не удаляет GalleryVersion или legacy payment audit.
24. Legacy mapping: 0 успешных доставок → 2 доступно; 1 → 1; 2 и больше → 0.
    До deploy в production находятся 2 owner/test account без реальных оплат: у них
    1 и 15 успешных доставок. Ожидаемая migration — 2 account, соответственно с
    остатком 1 и 0 credit; окончательный факт проверяется post-deploy audit без ID.

## Verification

25. Локальные regression tests проверяют initial grant, разные фото, Correction/Repeat,
    failures, concurrency, callback replay, package grant, old/new/foreign version,
    original delivery reserve/release/restart recovery, refund, migration,
    site/legal/receipt и telemetry privacy. Результат: 254 backend + 49 site — OK.
26. Общее число автоматических tests: 303, дополнительно 36 subtests; compile,
    health, dependency check, secret scan, asset audit, HTTP smoke и Lighthouse
    budgets (100/100/100/100 mobile и desktop) прошли.
27. Commit фиксируется в итоговом operational handoff: документ не может надёжно
    содержать собственный Git SHA без рекурсивного изменения этого SHA.
28. GitHub Actions run фиксируется в итоговом operational handoff после завершения CI.
29. Production SHA читается из `/opt/photo-bot/data/deployed_commit.txt` и
    фиксируется в итоговом operational handoff после deploy.
30. Site SHA читается из `/opt/pixora-site/deployed_commit.txt` и фиксируется там же.
31. Требуемое production значение `MAX_POLL_OBSERVE_ONLY=true`.
32. Требуемое production значение `PILOT_USER_LIMIT=0`.
33. Требуемые payment flags: payments/webhook/refunds/production approval false, provider disabled, sandbox.
34. Реальные платежи до deploy: 0; post-deploy audit повторно проверяет это значение.
35. OpenAI image requests в этом спринте: 0; workflow credential check тоже не выполняет внешний OpenAI request.
36. Сайт инженерно готов к повторной отправке на Robokassa moderation, но описание товара в кабинете и fiscal receipt settings проверяет владелец.
37. Sandbox E2E технически подготовлен, но не считается пройденным без отдельного разрешения и внешней операции.
38. Владельцу остаются: обновить описание товара/чека в Robokassa, пройти moderation, отдельно разрешить sandbox E2E, получить legal/fiscal review и только затем решать о реальных платежах и pilot handlers.
