# Карта настроек Robokassa

Этот документ описывает целевую конфигурацию, но не разрешает её включение. Источник протокола — официальные документы Robokassa: [платёжный интерфейс](https://docs.robokassa.ru/ru/pay-interface.html), [ResultURL и redirects](https://docs.robokassa.ru/ru/notifications-and-redirects.html), [тестовый режим](https://docs.robokassa.ru/ru/testing-mode), [фискализация](https://docs.robokassa.ru/ru/fiscalization.html), [Refund API](https://docs.robokassa.ru/ru/refund-api).

## Целевые значения

| Настройка | Значение |
|---|---|
| Merchant Login / Shop ID | значение из одобренного кабинета; пока неизвестно |
| Password #1 | секрет для подписи исходящей формы; только `.env` |
| Password #2 | секрет проверки ResultURL; только `.env` |
| Password #3 | секрет Refund API; обязателен до тестирования возврата |
| Алгоритм | SHA-256 в кабинете и `ROBOKASSA_HASH_ALGORITHM=sha256` |
| Encoding | UTF-8 |
| ResultURL | `https://pixoraai.ru/payments/robokassa/result` |
| ResultURL method | POST only |
| SuccessURL | `https://pixoraai.ru/legal/payment-refund.html?payment=success` |
| SuccessURL method | GET; информационный redirect, не подтверждает оплату |
| FailURL | `https://pixoraai.ru/legal/payment-refund.html?payment=failed` |
| FailURL method | GET; не меняет локальный статус на paid |
| Режим до go-live | sandbox / `IsTest=1` |
| Production approval | `ROBOKASSA_PRODUCTION_APPROVED=false` до отдельного решения владельца |

Выбран минимальный вариант: reverse proxy Nginx на том же домене и пути, loopback listener `127.0.0.1:8091`. Отдельный API-поддомен не нужен.

## Чек и предмет расчёта

Целевой receipt одной позиции:

- name: `Оригинал выбранной версии Pixora`;
- quantity: `1`;
- sum: `49.00`;
- payment_method: `full_payment`;
- payment_object: `service`;
- tax: значение `PAYMENT_RECEIPT_TAX`, подтверждённое владельцем и Robokassa.

Нельзя угадывать tax system/receipt tax. До sandbox E2E владелец должен письменно подтвердить, кто формирует чек для плательщика НПД, нужна ли облачная касса и какое значение `tax` принимает магазин. Текущая реализация умеет передавать receipt, но бизнес-настройка пока `OWNER ACTION`.

## Неактивируемый Nginx-фрагмент

Сначала пройти code review и unit tests. Только затем отдельным deploy:

```nginx
limit_req_zone $binary_remote_addr zone=robokassa_result:10m rate=10r/m;

location = /payments/robokassa/result {
    limit_except POST { deny all; }
    client_max_body_size 64k;
    limit_req zone=robokassa_result burst=10 nodelay;
    proxy_pass http://127.0.0.1:8091/payments/robokassa/result;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Request-ID $request_id;
    proxy_pass_request_headers on;
}
```

Ограничения: backend слушает только loopback; autoindex off; остальные backend endpoints не публикуются; query/body/signature не добавляются в access log; healthcheck остаётся отдельным локальным endpoint. Приложение принимает только POST, тело до 64 KiB, строгий UTF-8 и отвечает `OK<InvId>` только после commit.

## Переменные production

```dotenv
PAYMENT_PROVIDER=robokassa
ROBOKASSA_MODE=sandbox
ROBOKASSA_MERCHANT_LOGIN=<secret>
ROBOKASSA_PASSWORD1=<secret>
ROBOKASSA_PASSWORD2=<secret>
ROBOKASSA_PASSWORD3=<secret>
ROBOKASSA_HASH_ALGORITHM=sha256
PAYMENT_RESULT_URL=https://pixoraai.ru/payments/robokassa/result
PAYMENT_SUCCESS_URL=https://pixoraai.ru/legal/payment-refund.html?payment=success
PAYMENT_FAIL_URL=https://pixoraai.ru/legal/payment-refund.html?payment=failed
```

Для текущего безопасного режима `PAYMENTS_ENABLED=false`, `ROBOKASSA_WEBHOOK_ENABLED=false`, `PAYMENT_REFUNDS_ENABLED=false`, `ROBOKASSA_PRODUCTION_APPROVED=false` сохраняются без изменений.
