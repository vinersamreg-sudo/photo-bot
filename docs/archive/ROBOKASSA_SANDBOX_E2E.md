# Owner-only Robokassa sandbox E2E

Не выполнять без отдельного явного разрешения владельца. Лимит — один sandbox-инвойс 49 ₽, без реальных денег и без OpenAI-запросов.

## Предусловия

- кабинет использует SHA-256;
- тестовые MerchantLogin, Password #1 и Password #2 установлены без раскрытия;
- Receipt соответствует [решению поддержки](ROBOKASSA_SUPPORT_DECISION.md);
- public ResultURL GET=405 и disabled POST=503;
- сделан backup БД и `.env`;
- MAX доступен только владельцу, pilot=0;
- Refund API остаётся выключенным.

`payment_method`, `payment_object` и второй чек больше не являются блокерами.

## Проверка

| # | Действие | Ожидаемый результат |
|---|---|---|
| 1 | Создать owner-only заказ | одна pending order и ровно одна `receipt_type=payment` запись |
| 2 | Проверить ссылку | GET, `IsTest=1`, OutSum=49.00, Description 2+1, обязательный Receipt |
| 3 | Декодировать Receipt | после первого decode — подписанное представление; после второго — исходный UTF-8 JSON |
| 4 | Проверить JSON | одна позиция, name «Пакет доступа Pixora», quantity 1, sum 49.00, tax none; запрещённых полей нет |
| 5 | Завершить sandbox-платёж | браузерный redirect ничего не начисляет |
| 6 | Получить ResultURL POST | SHA-256 Password #2, invoice, amount и token валидны |
| 7 | Проверить commit | `OK<InvId>` только после paid + одного +2/+1 grant |
| 8 | Повторить callback | idempotent OK, без второго пакета и без второй sale receipt |
| 9 | Использовать одну обработку | credit уменьшается, новых receipt rows нет |
| 10 | Получить оригинал | entitlement уменьшается, новых receipt rows нет |
| 11 | Повторить доставку оригинала | доставка идемпотентна, новых receipt rows нет |
| 12 | Проверить SuccessURL/FailURL вручную | никакого paid/grant/receipt изменения |
| 13 | Перезапустить сервис | paid/grant/receipt состояние сохраняется |
| 14 | Reconcile | ноль локальных расхождений; статус вручную совпадает с кабинетом |
| 15 | Refund dry-run | ни provider-вызова, ни нового receipt row |

Команды проверки:

```bash
python -m app.main robokassa-health --format human
python -m app.main payment-show --invoice <INV> --format human
python -m app.main payment-reconcile --invoice <INV> --format human
```

После теста вернуть все payment flags в false, observe-only в true и pilot в 0. Evidence-файл не должен содержать user ID, секреты, подписанную ссылку, полный invoice или callback body.
