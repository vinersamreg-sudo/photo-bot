# Ravuna repository context

Repository: `photo-bot`. Public product brand: **Ravuna**. Production backend
root and systemd unit retain the technical name `/opt/photo-bot` / `photo-bot`.
Official site: `https://ravuna.ru`. Verified MAX username:
`https://max.ru/se13572368_bot` (username is not changed automatically).

## Product contract

Ravuna is an AI photo editor in MAX. The user sends a photo and a natural-language
instruction, receives a watermarked demo, and may obtain an original. User-facing
copy sells the result and uses “обработка”, never provider model names or
“generation”.

The public digital product is **«Пакет доступа Ravuna»** for 49 ₽:

- two processing operations;
- one original without a watermark;
- no subscription or automatic renewal.

Internal product code `continuation_pack_2_plus_1`, credit ledger, entitlement
ledger, SQLite schema and historical records are stable compatibility contracts.

## AI and lineage

Production uses OpenAI `gpt-image-2`. `SceneIntent` and `EditPlan` are
provider-neutral; the prompt builder emits an English/ASCII technical prompt.
Correction edits the selected successful version, Repeat reuses its intent and
input branch, and Ravuna's database remains the source of truth. Experimental
hybrid-processing routes remain disabled unless separately approved.

## Payments

Robokassa signatures use SHA-256. Public payment copy and Receipt item use
«Пакет доступа Ravuna». ResultURL is the only source of payment truth and grants
both package components atomically and idempotently.

Immutable payment compatibility:

- MerchantLogin is not a brand setting and must not be renamed;
- ResultURL remains `https://pixoraai.ru/payments/robokassa/result`;
- the legacy domain is served with Ravuna-branded static pages and only that
  exact backend proxy;
- password names, webhook contract, fiscal fields and provider modes are not
  changed by branding work.

## Site

`site/public` is the single Ravuna source tree. It contains no backend imports,
secrets, user files or runtime JavaScript. Canonical, Open Graph, Twitter Card,
JSON-LD, manifest, robots, sitemap, legal pages and payment return pages use
`ravuna.ru`. Production releases are atomic under `/opt/ravuna-site`.

For ResultURL compatibility the same Ravuna-branded artifact is also published
under the historical static root `/opt/pixora-site`; that root is not a second
brand or a separate product.

## MAX and other channels

MAX transport is the active user channel. Brand copy, onboarding, legal links,
gallery, history, payment and download messages use Ravuna. The registered MAX
username remains `se13572368_bot`; changing username or profile metadata requires
an explicit supported external operation.

There is no active Telegram transport in this repository. Content Studio may
prepare platform-neutral material, but external publishing is disabled by
default.

## Storage and data

SQLite plus private Storage hold users, sessions, works, versions, payment
intents, orders, receipts and ledgers. Do not rename persisted enum values,
database columns or stable anonymisation salts during a brand change. The
following legacy identifiers are intentionally retained:

- `created_for_pixora`;
- `pixora_owned`;
- `pixora-product-subject`;
- `pixora-mask-*` / `pixora-composite-*`;
- `pixora-*.sqlite3.enc`.

They are internal compatibility identifiers and never appear in user-facing
copy.

## Safe production defaults

Every ordinary deploy must return the system to:

- `MAX_POLL_OBSERVE_ONLY=true`;
- `PAYMENTS_ENABLED=false`;
- `PAYMENT_PROVIDER=disabled`;
- `PAYMENT_WEBHOOK_ENABLED=false`;
- `PAYMENT_REFUNDS_ENABLED=false`;
- `PILOT_USER_LIMIT=0`;
- `ROBOKASSA_MODE=sandbox`;
- `ROBOKASSA_PRODUCTION_APPROVED=false`.

Future tests must record the initial production state and restore exactly that
state. Never shut down the user's laptop unless the user gives a direct command
for that specific shutdown.

## Operator entry points

```text
python -m app.main healthcheck
python -m unittest discover -v
scripts/ravuna content status
scripts/ravuna content queue --status needs_review
```

Site deployment is gated by repository variable
`RAVUNA_SITE_DEPLOY_ENABLED=true`. Backend and site workflows are separate.

## Historical migration note

The predecessor public brand was Pixora. That name may remain only in Git
history, CHANGELOG, dated audit/migration evidence, immutable internal identifiers
listed above, the Robokassa ResultURL domain and its compatibility Nginx config.
It must not appear in current user copy, current SEO, current product docs or
new operational messages.
