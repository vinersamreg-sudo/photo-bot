# Ravuna — launch checklist

Актуально на 27.07.2026. Статусы: **BLOCKER**, **REQUIRED FOR PILOT**, **REQUIRED BEFORE PUBLIC**, **CAN WAIT**.

## BLOCKER

- [ ] Production secrets `ROBOKASSA_MERCHANT_LOGIN`, `ROBOKASSA_PASSWORD_1`, `ROBOKASSA_PASSWORD_2` добавлены в GitHub Environment `production`; проверяется только наличие.
- [ ] Временные `ROBOKASSA_TEST_PASSWORD_1` и `ROBOKASSA_TEST_PASSWORD_2` удалены после завершения sandbox-работ.
- [ ] Перед постоянным коммерческим запуском владелец отдельно разрешил production Robokassa mode и платежные флаги.
- [ ] Последний подтверждённый OpenAI balance записан с датой; warning/critical status просмотрен.
- [ ] Для открываемого этапа достаточно OpenAI balance с учётом worst-case request count.
- [ ] Последний deploy SHA совпадает с GitHub `main`.
- [ ] Healthcheck, SQLite `quick_check`, processing=0, orphan=0 и один runtime подтверждены после deploy.
- [x] Свежий encrypted backup, offsite copy и restore test подтверждены.

## REQUIRED FOR PILOT

- [ ] Три `pending` PaymentIntent с финально `expired` order закрыты через идемпотентный reconcile; audit сохранён.
- [ ] Ровно пять проверенных ID записаны в `MAX_PILOT_USER_IDS`; owner отсутствует и считается отдельно.
- [ ] `PILOT_USER_LIMIT=5` включается только после отдельного разрешения владельца.
- [x] Outsider получает закрытое сообщение и не создаёт session/file/OpenAI request.
- [ ] `openai-budget-status` и `monitoring-status` просмотрены перед открытием handlers.
- [x] Daily request limit и daily estimated cost limit настроены; перед пилотом владелец подтверждает их соответствие бюджету.
- [x] Emergency stop `OPENAI_IMAGE_REQUESTS_ENABLED=false` проверен тестом без provider request.
- [x] Support contact и порядок реакции на оплату/невыдачу оригинала готовы.
- [x] Legal offer, privacy, payment/refund pages и цена 49 ₽ доступны.
- [x] Test workflow сохраняет и восстанавливает точное исходное состояние production; fail-closed путь покрыт тестом.

## REQUIRED BEFORE PUBLIC

- [x] Реальный ResultURL SHA-256, один receipt, начисление +2/+1 и duplicate idempotency подтверждены на текущей production-конфигурации.
- [ ] P0/P1 monitoring имеет регулярного ответственного или внешний alert channel.
- [ ] Paid-without-grant, original retry, callback rejection и stale processing равны нулю.
- [ ] Contribution измерен на pilot cohort с учётом non-payers, refunds и всех OpenAI requests.
- [ ] Фактические USD/RUB, НПД, VPS, storage, traffic, support и refund reserve включены в экономику.
- [ ] Abuse/rate limits и cost stop проверены под ожидаемой публичной нагрузкой.
- [ ] Backup restore и exact-state restore повторно проверены после коммерческого включения.

## CAN WAIT

- [ ] Автоматическое чтение OpenAI balance — только через официальный поддерживаемый API, если такой появится.
- [ ] Внешний dashboard поверх privacy-safe CLI reports.
- [ ] Автоматическая оптимизация цены после накопления достаточной cohort-статистики.

Пилот и публичный режим не включаются этим чек-листом автоматически. Каждый rollout требует отдельного решения владельца.
