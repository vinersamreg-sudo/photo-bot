# Payment Security

## Implemented controls

- payments, business callbacks, refunds and production approval default off;
- SHA-256 is hard-locked; MD5, SHA-512 and other algorithms are rejected;
- Receipt is mandatory, non-empty, canonical and derived from integer 4900 minor units;
- Receipt has one item and omits `sno`, `payment_method`, `payment_object`, `cost` and advance markers;
- the exact once-encoded Receipt is signed; GET transports its twice-encoded form;
- regression tests detect a one-byte Receipt change, wrong encoding layer and broken Cyrillic recovery;
- ResultURL is POST-only, loopback-backed, size-limited and strict UTF-8;
- Password #2 verification uses constant-time comparison;
- exact invoice, amount, merchant/provider, currency and opaque token are checked;
- callback replay cannot create a second package or second sale receipt;
- browser redirects grant nothing;
- package usage and original delivery cannot create sale receipts;
- operator output excludes credentials, signatures, raw callback bodies and private identifiers.

## Required before sandbox

- Robokassa cabinet hash set to SHA-256;
- test MerchantLogin, Password #1 and Password #2 supplied without disclosure;
- exact ResultURL/SuccessURL/FailURL methods confirmed;
- explicit owner approval for one 49 ₽ sandbox invoice;
- DB and `.env` backup plus rollback plan.

## Required before production money

- recorded sandbox E2E and callback replay evidence;
- qualified review of offer, refund and fiscal wording;
- provider/local reconciliation and support procedure;
- production credentials and separate `ROBOKASSA_PRODUCTION_APPROVED=true` authorization;
- controlled first real owner transaction.

The written support decision resolves the former `payment_method`/`payment_object` blocker. It does not itself authorize a payment.
