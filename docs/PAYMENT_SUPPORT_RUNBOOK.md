# Поддержка платежей и споров

Главный принцип: browser redirect не является доказательством оплаты. Проверяем masked invoice в локальной БД и кабинете Robokassa; не просим номер карты, CVV, пароль или SMS-код.

## Первичная диагностика

```bash
cd /opt/photo-bot
.venv/bin/python -m app.main payment-show --invoice <INV> --format human
.venv/bin/python -m app.main payment-reconcile --invoice <INV> --format human
.venv/bin/python -m app.main payment-history --invoice <INV> --format human
.venv/bin/python -m app.main refund-history --format human
```

Запрашивать у пользователя можно: примерное время, сумму, masked invoice/чек Robokassa и последние четыре символа идентификатора операции из чека. Банковские секреты не нужны.

## Случаи

| Ситуация | Действие оператора |
|---|---|
| Оплатил, оригинал не пришёл | если paid/delivery_pending и original существует — сначала dry-run `payment-resend-original`, затем `--apply`; проверить audit |
| Повторный webhook | подтвердить идемпотентность; не создавать заказ/доставку вручную |
| Browser success, webhook отсутствует | сообщить «платёж проверяется»; сверить кабинет; не разблокировать по скриншоту |
| Сумма не совпала | callback должен быть rejected; эскалация, ручной unlock запрещён |
| Invoice неизвестен/просрочен | не создавать paid state; сверить магазин/время; предложить новый заказ только после расследования |
| Двойное списание | найти обе операции в кабинете; одну доставку не считать компенсацией; оформить возврат лишней операции |
| MAX не доставляет | paid сохраняется; retry после проверки доступности MAX и файла |
| Original удалён | попробовать зашифрованный backup; не подменять версию; при невозможности — возврат |
| GalleryVersion удалена после оплаты | восстановить точную версию/lineage или вернуть деньги; не выдавать sibling |
| Запрос на возврат | зафиксировать причину и сумму; выполнить `refund-create` без `--apply`, согласовать, затем отдельный apply/submit только при включённом gate |
| Refund initiated/failed | проверять `refund-status`; не обещать завершение до статуса провайдера |
| Кабинет и SQLite расходятся | остановить новые платежи, сохранить evidence, ручная сверка invoice/суммы/времени, затем исправление с audit |

## Безопасные команды восстановления

```bash
# Просмотр — без изменения
.venv/bin/python -m app.main payment-resend-original --invoice <INV>
.venv/bin/python -m app.main payment-mark-delivery-retry --invoice <INV>
.venv/bin/python -m app.main refund-create --invoice <INV> --amount-rub 49 --reason customer_request --idempotency-key support-<ticket> --dry-run

# Изменение — только после проверки и записи номера обращения
.venv/bin/python -m app.main payment-mark-delivery-retry --invoice <INV> --apply
.venv/bin/python -m app.main payment-resend-original --invoice <INV> --apply
```

## Шаблоны ответа

- «Оплата подтверждена. Мы повторно отправляем оригинал выбранной версии. Пожалуйста, проверьте диалог MAX.»
- «Платёж ещё проверяется. Оригинал остаётся заблокированным до подтверждения Robokassa; сообщим после сверки.»
- «Платёж с указанными данными пока не найден. Пришлите время, сумму и номер операции из чека Robokassa без реквизитов карты.»
- «Обнаружены две подтверждённые оплаты. Лишнюю операцию передали на возврат; отдельно сообщим её статус.»
- «Запрос на возврат принят в работу. Возврат считается завершённым только после подтверждения платёжного сервиса.»
- «Для проверки нужны время, сумма и номер операции из чека. Не присылайте номер карты, CVV, пароль или SMS-код.»

Любая потеря paid state, выдача чужой версии, повторное списание без восстановления или несогласованная сумма — P0: `PAYMENTS_ENABLED=false`, webhook/refunds off, сохранить логи без секретов и остановить rollout.

