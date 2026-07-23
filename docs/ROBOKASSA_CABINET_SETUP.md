# Карта настроек Robokassa

Этот документ описывает целевую конфигурацию, но не разрешает её включение. Источник протокола — официальные документы Robokassa: [платёжный интерфейс](https://docs.robokassa.ru/ru/pay-interface.html), [ResultURL и redirects](https://docs.robokassa.ru/ru/notifications-and-redirects.html), [тестовый режим](https://docs.robokassa.ru/ru/testing-mode), [фискализация](https://docs.robokassa.ru/ru/fiscalization.html), [Refund API](https://docs.robokassa.ru/ru/refund-api).

## Целевые значения

| Настройка | Значение |
|---|---|
| Merchant Login / Shop ID | значение из одобренного кабинета; пока неизвестно |
| Password #1 | секрет для подписи исходящей формы; только `.env` |
| Password #2 | секрет проверки ResultURL; только `.env` |
| Password #3 | секрет Refund API; обязателен до тестирования возврата |
| Алгоритм | Pixora реально использует SHA-256 (`ROBOKASSA_HASH_ALGORITHM=sha256`). В кабинете сейчас выбран MD5: это блокирующее расхождение, но в рамках аудита кабинет не изменяется |
| Encoding | UTF-8 |
| ResultURL | `https://pixoraai.ru/payments/robokassa/result` |
| ResultURL method | POST only |
| SuccessURL | `https://pixoraai.ru/payment-success.html` |
| SuccessURL method | GET; информационный redirect, не подтверждает оплату |
| FailURL | `https://pixoraai.ru/payment-failed.html` |
| FailURL method | GET; не меняет локальный статус на paid |
| Режим до go-live | sandbox / `IsTest=1` |
| Production approval | `ROBOKASSA_PRODUCTION_APPROVED=false` до отдельного решения владельца |

Выбран минимальный вариант: reverse proxy Nginx на том же домене и пути, loopback listener `127.0.0.1:8091`. Отдельный API-поддомен не нужен.

## Чек и предмет расчёта

Целевой Receipt одной позиции:

- name: `Пакет Pixora: 2 варианта обработки и 1 оригинал`;
- quantity: `1`;
- cost: `49.00`;
- sum: `49.00`;
- payment_method: пока пусто; требуется подтверждение Robokassa для Робочеков СМЗ;
- payment_object: пока пусто; требуется подтверждение Robokassa для Робочеков СМЗ;
- tax: `none` (документированное Robokassa значение «без НДС», соответствующее подтверждённому владельцем НПД).

Нельзя угадывать способ и предмет расчёта. Конфигурация Pixora не разрешает включить платежи, пока `PAYMENT_RECEIPT_PAYMENT_METHOD` и `PAYMENT_RECEIPT_PAYMENT_OBJECT` пусты. Робочеки СМЗ подтверждены владельцем как активные, одобренные ФНС и автоматически передающие чеки; внешний sandbox E2E чека ещё не выполнен.

## Публичный fail-closed Nginx transport

Прокси публикуется отдельным deploy без включения платежей:

```nginx
limit_req_zone $binary_remote_addr zone=robokassa_result:10m rate=30r/m;

location = /payments/robokassa/result {
    client_max_body_size 64k;
    limit_req zone=robokassa_result burst=20 nodelay;
    access_log off;
    proxy_pass http://127.0.0.1:8091/payments/robokassa/result;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Request-ID $request_id;
}
```

Ограничения: backend слушает только loopback; autoindex off; остальные backend endpoints не публикуются; query/body/signature не добавляются в access log; healthcheck остаётся отдельным локальным endpoint. Пока `PAYMENT_WEBHOOK_ENABLED=false`, GET отвечает 405, а POST — 503 без изменения данных. После отдельного sandbox-enable приложение принимает только POST, тело до 64 KiB, строгий UTF-8 и отвечает `OK<InvId>` только после commit.

Первичная или изменяющая Nginx публикация требует root и выполняется только проверенным
`ops/deploy_nginx_resulturl.sh`; обычный CI после этого проверяет 405/503 без права
перезаписывать root-конфигурацию.

## Переменные production

```dotenv
PAYMENT_PROVIDER=robokassa
ROBOKASSA_MODE=sandbox
ROBOKASSA_MERCHANT_LOGIN=<secret>
ROBOKASSA_PASSWORD1=<secret>
ROBOKASSA_PASSWORD2=<secret>
ROBOKASSA_PASSWORD3=<secret>
ROBOKASSA_HASH_ALGORITHM=sha256
PAYMENT_WEBHOOK_LISTENER_ENABLED=true
PAYMENT_WEBHOOK_ENABLED=false
PAYMENT_RESULT_URL=https://pixoraai.ru/payments/robokassa/result
PAYMENT_SUCCESS_URL=https://pixoraai.ru/payment-success.html
PAYMENT_FAIL_URL=https://pixoraai.ru/payment-failed.html
PAYMENT_RECEIPT_ITEM_NAME=Пакет Pixora: 2 варианта обработки и 1 оригинал
PAYMENT_RECEIPT_TAX=none
PAYMENT_RECEIPT_PAYMENT_METHOD=
PAYMENT_RECEIPT_PAYMENT_OBJECT=
```

Для текущего безопасного режима `PAYMENTS_ENABLED=false`, `PAYMENT_PROVIDER=disabled`, `PAYMENT_WEBHOOK_ENABLED=false`, `PAYMENT_REFUNDS_ENABLED=false`, `ROBOKASSA_PRODUCTION_APPROVED=false` сохраняются без изменений.

Нужны именно тестовые Password #1 и Password #2 из кабинета, совместимые с выбранным там SHA-256. Password #3/OpKey нужен только для отдельной проверки Refund API; возвраты сейчас выключены. Значения не выводятся и не сохраняются в Git.

## Один вопрос в поддержку до sandbox

> Для самозанятого на НПД с активными «Робочеками СМЗ», продающего цифровой продукт «Пакет доступа Pixora» с полной онлайн-оплатой до оказания услуги, какие точные значения `payment_method` и `payment_object` нужно передавать в `Receipt`, и требуется ли затем второй чек полного расчёта? Публичный продукт включает две обработки и один оригинал; существующее фискальное наименование Receipt в этой задаче не меняется.

До письменного ответа эти два поля остаются пустыми, а sandbox-платёж не запускается.

## Имена секретов для sandbox

В GitHub Environment `production` и затем в `/opt/photo-bot/.env` нужны следующие имена без вывода значений:

- `ROBOKASSA_MERCHANT_LOGIN`;
- `ROBOKASSA_PASSWORD1` — тестовый пароль №1 для SHA-256;
- `ROBOKASSA_PASSWORD2` — тестовый пароль №2 для SHA-256;
- `ROBOKASSA_PASSWORD3` — только если отдельно разрешена проверка Refund API.

На 22.07.2026 эти имена отсутствуют в списке GitHub production secrets; их наличие в production `.env` проверяется только как boolean, без чтения значений.
