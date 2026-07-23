# Robokassa Integration

Current source of truth: [ROBOKASSA_SUPPORT_DECISION.md](ROBOKASSA_SUPPORT_DECISION.md), the written Robokassa support decision received on 23.07.2026 for a self-employed merchant using active «Робочеки СМЗ». Protocol details are also checked against the official [payment interface](https://docs.robokassa.ru/ru/pay-interface.html), [redirects](https://docs.robokassa.ru/ru/notifications-and-redirects.html) and [fiscalization](https://docs.robokassa.ru/ru/fiscalization.html).

## Payment initialization

Pixora uses a classic GET payment link to `https://auth.robokassa.ru/Merchant/Index.aspx`. `RobokassaProvider` sends `MerchantLogin`, server-calculated `OutSum`, numeric `InvId`, `Description`, mandatory `Receipt`, expiry, `Shp_order`, optional URL2 return fields and SHA-256 `SignatureValue`. Sandbox adds `IsTest=1`.

The only sale item is:

```json
{"items":[{"name":"Пакет доступа Pixora","quantity":1,"sum":49.00,"tax":"none"}]}
```

It has no `sno`, `payment_method`, `payment_object`, `cost`, advance or prepayment marker. The amount source is integer `PRICE_MINOR=4900`; binary float is not a financial source of truth.

## Receipt encoding and Password #1 signature

The canonical UTF-8 JSON is stable, compact and keeps Cyrillic unescaped. It is URL-encoded once. That once-encoded value is included in the signature. Because Pixora sends `SuccessUrl2` and `FailUrl2`, the actual base is:

`MerchantLogin:OutSum:InvId:Receipt:SuccessUrl2:GET:FailUrl2:GET:Password1:Shp_order=...`

The encoded return URLs and alphabetically sorted `Shp_*` fields are the same values sent in the request. The GET query builder URL-encodes the already encoded `Receipt` a second time. One query decode returns the signed value; the second returns the original JSON. Tests reject signing the twice-encoded query value.

## ResultURL

Nginx forwards POST only to the loopback `/payments/robokassa/result`. GET returns 405. While `PAYMENT_WEBHOOK_ENABLED=false`, POST returns 503 without writing payment state.

When separately enabled for an approved sandbox, the backend verifies the Password #2 SHA-256 signature, exact 49.00 amount, `InvId`, local merchant/provider binding, RUB order and `Shp_order`. One SQLite transaction confirms the payment, creates one +2/+1 package grant and marks the single sale-receipt audit record confirmed. `OK<InvId>` is returned only after commit. Duplicate callbacks are idempotent.

SuccessURL and FailURL do not grant value. Receipt from the browser or callback is never the source of truth.

## One-check model

One payment buys one «Пакет доступа Pixora» and creates one sale receipt. Using either processing operation, selecting the original or redelivering it creates no new sale receipt. A separate `receipt_type=refund` database record is return audit only, not a second receipt of sale.

## Safety state

Deploy forces payments, business callbacks and refunds off, sandbox mode on, production approval off, MAX observe-only on for a normal push and pilot limit 0. SHA-256 is the only accepted algorithm. No payment or refund may be executed without separate owner approval and credentials.
