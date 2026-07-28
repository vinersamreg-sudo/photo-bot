# Карта настроек Robokassa

Этот документ не разрешает менять кабинет или запускать платёж. Фискальный source of truth: [официальный письменный ответ поддержки](ROBOKASSA_SUPPORT_DECISION.md).

## Целевые значения

| Настройка | Значение |
|---|---|
| Merchant Login | значение магазина |
| Password #1 | тестовый/production секрет для исходящей SHA-256 подписи |
| Password #2 | тестовый/production секрет для ResultURL SHA-256 |
| Password #3 | только для отдельно разрешённого Refund API |
| Алгоритм | SHA-256 |
| Encoding | UTF-8 |
| ResultURL | `https://pixoraai.ru/payments/robokassa/result`, POST |
| SuccessURL | `https://ravuna.ru/payment-success.html`, GET |
| FailURL | `https://ravuna.ru/payment-failed.html`, GET |
| До go-live | sandbox / `IsTest=1` |

## Receipt

```json
{"items":[{"name":"Пакет доступа Ravuna","quantity":1,"sum":49.00,"tax":"none"}]}
```

`sno`, `payment_method`, `payment_object`, `cost`, `prepayment`, `full_prepayment`, `advance` и `full_payment` отсутствуют. Это одна цифровая услуга, один заказ, один платёж и один чек продажи. Состав 2+1 указывается в `Description`, на сайте и в оферте.

## Production environment names

```dotenv
PAYMENT_PROVIDER=robokassa
ROBOKASSA_MODE=sandbox
ROBOKASSA_MERCHANT_LOGIN=<secret>
ROBOKASSA_PASSWORD1=<secret>
ROBOKASSA_PASSWORD2=<secret>
ROBOKASSA_PASSWORD3=<only-for-approved-refund-test>
ROBOKASSA_HASH_ALGORITHM=sha256
PAYMENT_WEBHOOK_LISTENER_ENABLED=true
PAYMENT_WEBHOOK_ENABLED=false
PAYMENT_RESULT_URL=https://pixoraai.ru/payments/robokassa/result
PAYMENT_SUCCESS_URL=https://ravuna.ru/payment-success.html
PAYMENT_FAIL_URL=https://ravuna.ru/payment-failed.html
PAYMENT_RECEIPT_ITEM_NAME=Пакет доступа Ravuna
PAYMENT_RECEIPT_TAX=none
```

Переменных `PAYMENT_RECEIPT_PAYMENT_METHOD` и `PAYMENT_RECEIPT_PAYMENT_OBJECT` больше нет. `sno` также не настраивается.

## Текущее безопасное состояние

`MAX_POLL_OBSERVE_ONLY=true`, `PILOT_USER_LIMIT=0`, `PAYMENTS_ENABLED=false`, `PAYMENT_PROVIDER=disabled`, `PAYMENT_WEBHOOK_ENABLED=false`, `PAYMENT_REFUNDS_ENABLED=false`, `ROBOKASSA_MODE=sandbox`, `ROBOKASSA_PRODUCTION_APPROVED=false`.

Публичный transport остаётся fail-closed: GET ResultURL — 405, POST — 503. Никаких платежей до отдельного owner-only sandbox-разрешения.

## Secrets, необходимые для sandbox

- `ROBOKASSA_MERCHANT_LOGIN`;
- тестовый `ROBOKASSA_PASSWORD1`, совместимый с SHA-256 кабинета;
- тестовый `ROBOKASSA_PASSWORD2`, совместимый с SHA-256 кабинета.

`ROBOKASSA_PASSWORD3` нужен только для отдельного теста возврата. Секреты не выводятся и не сохраняются в Git.
