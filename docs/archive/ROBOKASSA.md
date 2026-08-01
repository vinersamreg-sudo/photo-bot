# Robokassa Integration

Current fiscal source of truth: [ROBOKASSA_SUPPORT_DECISION.md](ROBOKASSA_SUPPORT_DECISION.md), the written Robokassa support decision received on 23.07.2026 for a self-employed merchant using active «Робочеки СМЗ». Protocol behavior is covered by regression tests and the official Robokassa payment, redirect and fiscalization documentation.

## Payment initialization

Ravuna uses the classic GET payment form at `https://auth.robokassa.ru/Merchant/Index.aspx`. `RobokassaProvider` sends:

- `MerchantLogin`;
- server-calculated `OutSum`;
- numeric `InvId`;
- `Description`;
- mandatory `Receipt`;
- `SignatureValue`;
- `Culture=ru`;
- `Encoding=utf-8`;
- `ExpirationDate`;
- `Shp_order`;
- `IsTest=1` only in sandbox.

Dynamic `SuccessUrl2`, `SuccessUrl2Method`, `FailUrl2` and `FailUrl2Method` are intentionally not sent. Browser return pages come from the static technical settings of the Robokassa shop. They are informational and never grant value.

`ExpirationDate` is rendered as Moscow wall-clock time in `YYYY-MM-DDThh:mm` format. The backend converts a timezone-aware deadline to UTC+03 before removing the offset.

## Receipt and Password #1

The only sale item is:

```json
{"items":[{"name":"Пакет доступа Ravuna","quantity":1,"sum":49.00,"tax":"none"}]}
```

It has no `sno`, `payment_method`, `payment_object`, `cost`, advance or prepayment marker. The amount source is integer `PRICE_MINOR=4900`; binary float is not a financial source of truth.

The canonical UTF-8 Receipt JSON is compact and keeps Cyrillic unescaped. It is URL-encoded once for the signing value. The SHA-256 base is:

`MerchantLogin:OutSum:InvId:Receipt:Password1:Shp_order=...`

`Shp_*` fields are appended in alphabetical order. The query builder encodes the already encoded `Receipt` once more; after one query decode Robokassa receives the once-encoded value that was signed.

## ResultURL

The production callback remains the established compatibility endpoint:

`https://pixoraai.ru/payments/robokassa/result`

This URL is an immutable payment integration boundary, not a public brand surface. Nginx forwards POST only to the loopback webhook service. GET returns 405. While `PAYMENT_WEBHOOK_ENABLED=false`, POST returns 503 without writing payment state.

When separately enabled, the backend verifies the Password #2 SHA-256 signature, exact amount, `InvId`, local merchant/provider binding, currency and `Shp_order`. One SQLite transaction confirms the payment, creates one +2/+1 package grant and confirms the single sale-receipt audit record. `OK<InvId>` is returned only after commit. Duplicate callbacks are idempotent.

The success and failure browser pages do not grant credits or originals and never mark an order paid.

## One-check model

One payment buys one «Пакет доступа Ravuna» and creates one sale receipt. Processing, original selection and original redelivery create no additional sale receipt. A separately approved refund writes refund audit only.

## Safety state

Normal deploys keep payments, business callbacks and refunds disabled, Robokassa in sandbox, production approval false, MAX observe-only and pilot limit zero. SHA-256 is the only accepted signature algorithm. No payment or refund is executed without separate owner approval.

Historical signature diagnostics remain in [ROBOKASSA_SIGNATURE_BISECT.md](ROBOKASSA_SIGNATURE_BISECT.md); they are not the production payment builder.
