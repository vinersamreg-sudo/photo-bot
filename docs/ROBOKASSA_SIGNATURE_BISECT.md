# Robokassa Signature Bisect

This is a local diagnostic for provider error 29. It generates five signed
sandbox URLs but never opens them, creates no `PaymentIntent`, reads no
application database and performs no callback.

## Secret boundary

The command accepts only the test merchant login and test Password #1 through
the current process environment or an explicit untracked dotenv file:

```powershell
python -m scripts.robokassa_signature_bisect `
  --env-file C:\private\pixora-robokassa-test.env
```

Required names:

```dotenv
ROBOKASSA_MERCHANT_LOGIN=...
ROBOKASSA_TEST_PASSWORD_1=...
```

Alternatively, enter the values locally without displaying Password #1:

```powershell
python -m scripts.robokassa_signature_bisect --prompt
```

The output is written under ignored `temp/robokassa-signature-bisect` by
default. Password #1 is used only in memory and is represented as `[REDACTED]`
in both reports. Password #2 is not needed because the diagnostic does not
execute or receive ResultURL.

## Variants

- A: base payment parameters and SHA-256 signature.
- B: A plus Receipt.
- C: B plus `Shp_order`.
- D: C plus Success/Fail URL2 and their methods.
- E: D plus `ExpirationDate`.

Every variant gets a new numeric `InvId`. The reports contain the exact ordered
parameter list, redacted signature base, once-encoded Receipt, raw Receipt query
value, SHA-256 digest and URL.

Open A through E manually and stop at the first URL returning error 29. Opening
a URL is an external sandbox operation and is deliberately outside this tool.

## ReturnURL modifier rule

Robokassa documents the canonical modifier order as:

`Receipt`, `StepByStep`, `ResultUrl2`, `SuccessUrl2`,
`SuccessUrl2Method`, `FailUrl2`, `FailUrl2Method`, `Token`.

Modifiers are added only when present. Therefore absent `StepByStep` and
`ResultUrl2` do not leave empty `::` positions. Only a missing `InvId` requires
an empty slot, and the bisect always supplies `InvId`.

For variants D and E the exact redacted shape is:

`MerchantLogin:OutSum:InvId:Receipt:SuccessUrl2:GET:FailUrl2:GET:[REDACTED]:Shp_order=...`

`ExpirationDate`, `Description` and `IsTest` are query parameters but are not
part of `SignatureValue`.

Official references:

- <https://docs.robokassa.ru/ru/pay-interface.html>
- <https://docs.robokassa.ru/ru/notifications-and-redirects.html>
