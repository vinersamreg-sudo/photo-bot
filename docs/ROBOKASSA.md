# Robokassa Integration

Implementation follows the classic payment form and ResultURL contract from the official [payment interface](https://docs.robokassa.ru/ru/pay-interface) and [notification documentation](https://docs.robokassa.ru/ru/notifications-and-redirects). Refund preparation follows the official [Refund API](https://docs.robokassa.ru/ru/refund-api).

## Payment form

`RobokassaProvider` builds `https://auth.robokassa.ru/Merchant/Index.aspx` with `MerchantLogin`, server-calculated `OutSum`, numeric `InvId`, receipt, expiry, `Shp_order` opaque token and a Password1 signature. Sandbox adds `IsTest=1`. Passwords never enter the URL, database audit or logs.

## ResultURL

The public HTTPS proxy must forward GET/POST form fields to the loopback path `/payments/robokassa/result`. The application validates the Password2 signature, known invoice, exact amount, local merchant/provider binding, RUB order, internal expiry, opaque order token and idempotency digest. It returns `OK<InvId>` only after the SQLite transaction commits.

The classic ResultURL does not itself provide a separately signed merchant field, currency, success status or provider timestamp. Therefore merchant/currency are validated against the locally created order; success is the documented meaning of a valid ResultURL; timestamp protection is the internal order expiry. These checks must not be described as ResultURL2/JWS validation.

Duplicate callbacks are safe. An exact replay returns the prior acknowledgement without another unlock or delivery. A second valid callback for an already paid order is audited as a duplicate payment and also cannot widen unlock scope.

## Refund limitation

Robokassa refund execution needs an operation key (`OpKey`) and Password3. Classic ResultURL may not supply `OpKey`; until ResultURL2 or a documented operation-status reconciliation supplies it, automatic refund submission is intentionally blocked. A local `RefundIntent` can still be prepared and audited for manual handling.

## Rollout

Production mode additionally requires `ROBOKASSA_PRODUCTION_APPROVED=true`; deployment never sets it automatically. Provider endpoints, signatures and receipts must be rechecked against the merchant cabinet before activation because cabinet settings are external state.
