# Robokassa Integration

Implementation follows the classic payment form and ResultURL contract from the official [payment interface](https://docs.robokassa.ru/ru/pay-interface) and [notification documentation](https://docs.robokassa.ru/ru/notifications-and-redirects). Refund preparation follows the official [Refund API](https://docs.robokassa.ru/ru/refund-api).

## Payment form

`RobokassaProvider` builds `https://auth.robokassa.ru/Merchant/Index.aspx` with `MerchantLogin`, server-calculated `OutSum`, numeric `InvId`, receipt, expiry, `Shp_order` opaque token and a Password1 signature. Sandbox adds `IsTest=1`. Passwords never enter the URL, database audit or logs.

## ResultURL

The public HTTPS proxy must forward **POST only** to the loopback path `/payments/robokassa/result`. GET on the configured path returns 405; bodies over 64 KiB or malformed UTF-8 are rejected. The application validates the Password2 signature, known invoice, exact amount, local merchant/provider binding, RUB order, internal expiry, opaque order token and idempotency digest. It returns `OK<InvId>` only after the SQLite transaction commits.

The classic ResultURL does not itself provide a separately signed merchant field, currency, success status or provider timestamp. Therefore merchant/currency are validated against the locally created order; success is the documented meaning of a valid ResultURL; timestamp protection is the internal order expiry. These checks must not be described as ResultURL2/JWS validation.

Duplicate callbacks are safe. An exact replay returns the prior acknowledgement without another unlock or delivery. A second valid callback for an already paid order is audited as a duplicate payment and also cannot widen unlock scope.

## Product grant

The only v1 item is `continuation_pack_2_plus_1`, receipt name «Пакет Pixora: 2 варианта обработки и 1 оригинал», quantity 1, cost 49.00 RUB and sum 49.00 RUB. Receipt money is rendered from integer `price_minor=4900`; binary floating-point is not used. A verified ResultURL atomically grants two generation credits and one available original entitlement. It does not unlock or deliver a version. The user later selects an owned available GalleryVersion; browser SuccessURL never grants value. Replayed callbacks return idempotent success without a second package.

Every payment link includes a URL-encoded `Receipt`. Its encoded value participates in the Password1 signature in the documented order. `tax=none` maps the owner-confirmed NPD/no-VAT sale to Robokassa's documented “without VAT” value. `payment_method` and `payment_object` remain deliberately unset until Robokassa confirms the exact Робочеки СМЗ values; configuration refuses to enable payments while either field is empty.

## Current transport and hash gate

The repository publishes only a fail-closed ResultURL transport while payments are disabled. `PAYMENT_WEBHOOK_LISTENER_ENABLED=true` starts a loopback listener; `PAYMENT_WEBHOOK_ENABLED=false` makes valid-path POST requests return `503 Retry-After` without parsing a signature, writing payment state, granting a package or returning `OK<InvId>`. GET returns `405 Allow: POST`. Business processing is enabled only in a separately approved sandbox window.

Pixora signs and verifies with `ROBOKASSA_HASH_ALGORITHM=sha256`. The cabinet was observed using MD5, so a payment test is blocked until the cabinet algorithm and Password1/Password2 test credentials are deliberately aligned with SHA-256. No cabinet setting was changed by this audit.

## Refund limitation

Robokassa refund execution needs an operation key (`OpKey`) and Password3. Classic ResultURL may not supply `OpKey`; until ResultURL2 or a documented operation-status reconciliation supplies it, automatic refund submission is intentionally blocked. A local `RefundIntent` can still be prepared and audited for manual handling.

## Rollout

Production mode additionally requires `ROBOKASSA_PRODUCTION_APPROVED=true`; deployment never sets it automatically. Provider endpoints, signatures and receipts must be rechecked against the merchant cabinet before activation because cabinet settings are external state.

Exact target fields and the fail-closed Nginx transport are documented in [ROBOKASSA_CABINET_SETUP.md](ROBOKASSA_CABINET_SETUP.md). A passing unit suite is not sandbox evidence; use [ROBOKASSA_SANDBOX_E2E.md](ROBOKASSA_SANDBOX_E2E.md).
