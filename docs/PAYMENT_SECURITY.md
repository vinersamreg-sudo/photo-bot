# Payment Security

## Implemented controls

- all commercial flags default off and deploy forcibly restores safe state;
- production activation requires a separate approval flag;
- ResultURL binds loopback, accepts only POST, caps bodies at 64 KiB and requires strict UTF-8;
- signatures use constant-time comparison; invoice, exact 49.00 RUB amount, merchant/provider, expiry and opaque token are checked;
- event digests and unique constraints provide callback/payment grant replay protection;
- ResultURL atomically grants the complete +2 credit / +1 entitlement package or nothing;
- browser redirect grants nothing; purchase never auto-selects or unlocks a version;
- generation credits reserve before provider and cannot go negative under concurrent requests;
- original unlock checks ownership, deletion and file presence before entitlement consumption;
- original entitlement is reserved before MAX delivery, consumed only after success,
  and returned to available on delivery failure;
- unused-package rollback is scoped to its source lot; used value requires manual review;
- admin adjustments require reason, idempotency key, dry-run default and explicit `--apply`;
- callbacks, telemetry, CLI and reports exclude credentials, signatures, raw bodies, MAX IDs, prompts, images and private paths;
- refund execution has an independent disabled flag and requires Password3 plus operation key.

## Required before activation

- trusted TLS ResultURL, strict host/path proxy, firewall and rate/body limits;
- protected environment approvals and secret rotation procedure;
- sandbox evidence for callback replay, two simultaneous generations, two payments, unlock race and refund hold/rollback;
- reconciliation between Robokassa cabinet, order, package grant, credit lot, entitlement, receipt and refund;
- alerts for rejected callbacks, negative/inconsistent balance, held packages and original retry backlog;
- backup/restore after migration v9 and documented incident response;
- accountant/lawyer approval of receipt nomenclature, tax and offer/refund terms.

## Known boundary

Classic ResultURL does not carry every ResultURL2/JWS field. Signature + amount + invoice + `Shp_order` and local merchant/currency/expiry are the implemented authority. Do not claim ResultURL2 protections. SQLite and one runtime process are appropriate for bounded owner/small pilot, not proven for mass public concurrency.
