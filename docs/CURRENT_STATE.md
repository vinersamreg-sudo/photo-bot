# Ravuna current state

Updated for the Ravuna rebrand release candidate. This document describes active behavior; dated implementation evidence remains in `CHANGELOG.md` and the historical audit documents.

## Product

- Public brand: Ravuna.
- Technical repository and systemd unit: `photo-bot`.
- Primary channel: MAX.
- Public site: `https://ravuna.ru`.
- Price: 49 ₽.
- Product: «Пакет доступа Ravuna» containing two photo processing operations and one original without a watermark.
- No subscription or automatic renewal.

## User flow

The primary flow is intentionally compact: start → photo with description → one preview → original/payment or correction/repeat. User-facing copy says «обработка», not «генерация». Gallery and version lineage retain all delivered results.

## AI and storage

OpenAI `gpt-image-2` remains the single image provider. SceneIntent and the English technical prompt builder preserve explicit constraints and parent-version lineage. Source images, previews and originals are stored privately; MAX receives only files authorized for the current user. No OpenAI configuration changes are part of the brand migration.

## Payments

- Robokassa uses SHA-256 only.
- Receipt: `{"items":[{"name":"Пакет доступа Ravuna","quantity":1,"sum":49.00,"tax":"none"}]}`.
- Payment link signature: `MerchantLogin:OutSum:InvId:Receipt:Password1:Shp_order=...`.
- Dynamic ReturnURL fields are not sent.
- Static success and failure pages use `ravuna.ru`.
- The established ResultURL remains `https://pixoraai.ru/payments/robokassa/result` as an immutable external integration boundary.
- ResultURL is the only source of truth for entitlement grants; browser pages never grant value.

## Safe production defaults

Normal deploy keeps:

```text
MAX_POLL_OBSERVE_ONLY=true
PILOT_USER_LIMIT=0
PAYMENTS_ENABLED=false
PAYMENT_PROVIDER=disabled
PAYMENT_WEBHOOK_ENABLED=false
PAYMENT_REFUNDS_ENABLED=false
ROBOKASSA_MODE=sandbox
ROBOKASSA_PRODUCTION_APPROVED=false
```

Tests or owner-only exercises must restore the exact state that existed before the exercise. The laptop is never shut down unless the owner gives a new direct shutdown command.

## Compatibility identifiers

Persisted/internal values such as `created_for_pixora`, `pixora_owned`, `pixora-product-subject`, `pixora-mask-*`, `pixora-composite-*`, backup prefixes and the old ResultURL are intentionally unchanged. They are not public brand copy and changing them would risk database, storage, backup or payment compatibility.
