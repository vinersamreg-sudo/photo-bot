# Закрытый пилот на 5 пользователей

Пилот подготовлен, но не включён. Он проверяет обработку фото, UX и поддержку и может проходить с платежами полностью выключенными.

## Stage 0 — безопасное состояние

`MAX_POLL_OBSERVE_ONLY=true`, `PILOT_USER_LIMIT=0`, payment/webhook/refunds false. Выполнить `launch-status --strict`, `openai-budget-status`, `monitoring-status`, backup/restore/off-site, SQLite quick_check, processing=0, orphan=0.

## Stage 1 — owner regression

Через ручной workflow `deploy.yml` включить `enable_owner_handlers=true`, `pilot_user_limit=0`, при необходимости reset owner dialog. Пройти start → upload → prompt → preview → correction/repeat → gallery/history. После PASS вернуть обычным deploy observe-only.

## Stage 2 — пять allowlisted пользователей

1. Каждый тестировщик пересылает сообщение боту определения MAX ID; владелец сверяет личность вне публичного чата.
2. Внести ровно пять ID в GitHub Environment secret `MAX_PILOT_USER_IDS` в нужном порядке. Это изменение allowlist не требует кода. GitHub фиксирует факт изменения secret, а значения не выводятся.
3. Проверить, что owner ID отсутствует в pilot secret и нет дублей.
4. Запустить вручную `deploy.yml`: handlers enabled, `pilot_user_limit=5`; payment inputs оставить выключенными.
5. На VPS проверить `pilot-status`, `pilot-report`, `launch-status`, single PID и `.env` без вывода значений allowlist.
6. Удаление пользователя: убрать его из secret, заменить проверенным участником или вернуться в Stage 0; повторно deploy. Не редактировать БД вручную.

Owner хранится отдельно в `MAX_OWNER_USER_ID`, не входит в лимит пяти pilot users и запрещён в `MAX_PILOT_USER_IDS`.

Остановить новые обработки: немедленно выполнить обычный push/manual deploy с handlers disabled и `pilot_user_limit=0`; убедиться в `MAX_POLL_OBSERVE_ONLY=true`. Уже начатую операцию не убивать без проверки целостности; дождаться timeout/recovery, затем processing=0.

## Наблюдение и бюджет

```bash
.venv/bin/python -m app.main pilot-status --format human
.venv/bin/python -m app.main pilot-report --format human
.venv/bin/python -m app.main cost-status --format human
.venv/bin/python -m app.main health-report --format human
.venv/bin/python -m app.main openai-budget-status --format human
.venv/bin/python -m app.main monitoring-status --format human
```

Проверять после каждого дня: start → photo → prompt → result → unlock → payment → original, first-result completion, average/p50/p95 duration, corrections/repeats, feedback, error reasons, estimated provider cost. Raw prompts, photos, paths и MAX IDs в отчёт не попадают. Ограничение schema v8: ранний `/start` без session/attempt/gallery связи не всегда можно отнести к cohort; это отмечается в report.

Стоп-условия: чужой original, потеря данных/paid state, повторяющийся stuck processing, delivery failure >5%, failed restore, неконтролируемые расходы, abuse, пользователь вне allowlist получил handler, first-result completion <80% после диагностики.

## Сообщение тестировщику

> Вы приглашены в закрытый тест Ravuna. Используйте свою фотографию, на которую у вас есть права. Доступны две успешно доставленные обработки с водяным знаком на одной или разных фотографиях; результат создаётся автоматически и может отличаться от ожиданий. Пакет доступа Ravuna за 49 ₽ сейчас не продаётся; после запуска в него будут входить две обработки и один оригинал. Оставьте оценку кнопками после результата, удалите работу через «Мои работы», а при проблеме напишите на viner-89@mail.ru без отправки банковских данных.
