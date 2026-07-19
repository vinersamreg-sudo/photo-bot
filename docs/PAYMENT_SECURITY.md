# Payment Security

## Implemented controls

- all commercial flags default off and deploy forcibly restores the safe state;
- production activation requires a second explicit approval flag;
- ResultURL listener binds loopback in production and accepts only its configured path;
- request bodies are capped at 64 KiB;
- signatures use constant-time comparison;
- invoice, exact amount, local provider/merchant, RUB, expiry and opaque token are checked;
- event digests, order status and database constraints provide replay/idempotency protection;
- callback source is hashed; safe payload stores booleans only;
- credentials, signatures, raw callback body, MAX IDs, prompts and private paths are excluded from logs/reports;
- unlock is exact-version and committed before delivery;
- failed delivery preserves paid state and supports re-delivery;
- refund execution has an independent disabled flag and requires Password3 plus operation key.

## Required infrastructure controls before activation

- TLS on the public ResultURL, strict host/path routing and proxy body/time limits;
- rate limiting and provider IP filtering where Robokassa publishes stable ranges;
- firewall blocking direct public access to the loopback port;
- secret rotation procedure and protected GitHub Environment approvals;
- alerting on rejected callbacks, duplicate payments and delivery backlog;
- daily reconciliation between Robokassa cabinet, orders, receipts and refunds;
- tested database backup after migration v8 and documented incident response.

## Known boundary

Classic ResultURL cannot prove a provider timestamp/currency/merchant field that it does not send. Local order binding and expiry mitigate substitution, while signature + amount + invoice + `Shp_order` are authoritative request checks. Do not claim ResultURL2/JWS protections until that integration is actually implemented and tested.
