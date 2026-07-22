# Current State

## 22.07.2026 — Robokassa ResultURL and Receipt hardening

- the permanent product name is «Пакет Pixora: 2 варианта обработки и 1 оригинал — 49 ₽» across MAX, site, offer, payment terms, Receipt and payment documentation;
- Receipt contains one item, quantity 1, cost 49.00 and sum 49.00, all derived from integer `price_minor=4900`; Receipt participates in the Password1 signature;
- Pixora is hard-locked to SHA-256: configuration rejects MD5/SHA-512 and the provider calls SHA-256 directly; the Robokassa cabinet was observed on MD5, so sandbox payment is blocked until the owner deliberately aligns the cabinet and test passwords;
- Nginx and the loopback listener publish only a fail-closed ResultURL transport: GET 405, disabled POST 503, no ACK, state mutation or package grant;
- business payment processing, refunds, pilot and public handlers remain off; mode remains sandbox and observe-only remains true;
- Робочеки СМЗ are owner-confirmed active/FNS-approved with automatic receipt transmission, but exact `payment_method` and `payment_object` are not confirmed by official documentation and remain required empty gates;
- no Robokassa cabinet setting, sandbox payment, real payment, refund or OpenAI image request was performed in this audit.

## 21.07.2026 — Pixora Content Studio v1 implemented, dry-run only

- добавлен независимый операторский pipeline DemoAsset → Transformation → Result →
  Post → review → schedule → publication audit;
- separate SQLite schema v2 и private storage не читают и не меняют user, Gallery,
  AI Brain или payment данные;
- Pillow собирает square/vertical/stories Before/After, русский copy строится только
  из deterministic templates с обязательными disclosure, CTA и UTM;
- каждый материал входит в `needs_review`; quality flags блокируют approval, а
  result/post/review утверждаются одной транзакцией;
- MAX publisher имеет preview/dry-run/manual/publish/retry interface, но production
  принудительно оставляет publishing false и не конфигурирует transport;
- реальных демонстрационных материалов, MAX-публикаций и OpenAI image requests в
  этом спринте нет;
- локальный gate: 278 backend tests + 49 site tests, compile, dependency, secret,
  asset-licence и deploy-policy checks зелёные;
- до первого реального asset нужен отдельный encrypted restore-tested backup для
  Content Studio SQLite и storage.

## 20.07.2026 — permanent Pixora v1 product model implemented

- migration v9 adds lifetime initial grant, global generation balances, credit lots/reservations/ledger, atomic continuation-pack grants and independent unlock entitlements;
- initial balance is exactly two successfully delivered previews across one or many photographs; all technical/policy/storage/delivery failures release the reservation;
- one verified `continuation_pack_2_plus_1` payment for 49 ₽ grants +2 generations and +1 user-selected original, without automatic unlock; repeat packs stack;
- original entitlement can be consumed on an owned available version created before
  or after purchase; missing original consumes nothing, MAX delivery uses a temporary
  reservation, and failure/restart returns the entitlement without unlocking the version;
- unused package rollback is automatic-safe; any used generation or original requires manual refund review;
- backend, MAX, site, offer, receipt description, CLI, telemetry and economics now use the same model;
- no real payment, OpenAI image request or public handler was enabled for this change.

## 20.07.2026 — payment/pilot readiness hardening

- Robokassa moderation package is complete and public site/legal routes were rechecked; standard declared SVG favicon works, while the optional `/favicon.ico` fallback remains a non-blocking 404.
- ResultURL is POST-only with 64 KiB and strict UTF-8 limits; its public transport is fail-closed while business callbacks remain disabled.
- Added privacy-safe payment show/reconciliation, dry-run-first resend/retry/refund commands, Robokassa health and cohort-scoped pilot report.
- `launch-status` now separates site, sandbox, production payment, owner E2E, pilot 5 and always-false public readiness.
- Sandbox, refunds and real payment are still unverified external gates; all commercial flags, handlers and pilot remain off.
- Unit economics at 49 ₽ remain fragile: at the working 10 ₽ generation estimate, 1.5 average free and 1.5 paid generations per package require about 60.9% conversion to break even; actual invoice data are still absent.

Runbooks: `ROBOKASSA_SUBMISSION_PACKAGE.md`, `PAYMENT_GO_LIVE_AUDIT.md`, `ROBOKASSA_CABINET_SETUP.md`, `ROBOKASSA_SANDBOX_E2E.md`, `PAYMENT_SUPPORT_RUNBOOK.md`, `PILOT_5_USERS_RUNBOOK.md`, `PILOT_UNIT_ECONOMICS.md`.

Актуально на 19.07.2026 для commercial MVP candidate. Production deployment обязан оставлять AI experiments и реальные платежи выключенными.

## Подтверждено в production

- deployed commit атомарно фиксируется workflow в `data/deployed_commit.txt`; фактический SHA проверяется post-deploy audit;
- `photo-bot.service` и MAX polling здоровы; `MAX_POLL_OBSERVE_ONLY=true`;
- owner allowlist настроен, pilot limit 0, пользовательские handlers выключены;
- OpenAI `gpt-image-2` — единственный production image provider; router/composite/segmentation выключены;
- SQLite migration v9, `PRAGMA quick_check=ok`, pending/processing attempts 0, processing dialogs 0, processing GalleryVersions 0 проверяются post-deploy audit;
- owner dialog восстановлен в `main_menu`;
- encrypted backup/restore/off-site lifecycle подтверждён; public launch readiness остаётся false.

## Реальный owner E2E

- выполнено ровно 5/5 разрешённых production image requests через MAX;
- все 5 attempts завершились `succeeded`, все previews доставлены с watermark;
- созданы GalleryVersions 11–15; quota списана ровно по одной попытке на доставленную версию;
- проверены три Correction, Repeat, Gallery, History, Favorite и Current best;
- средняя provider duration 63.824 с; внутренний cost reserve 50 RUB;
- identity оставалась узнаваемой; recolor куртки и correction резкости сработали;
- остаётся visual drift: Repeat заметно изменил композицию, а первый background edit не дал требуемую резкость сразу;
- разговорный parser fallback в повторной серии не проверялся из-за жёсткого лимита; semantic parser не добавлен.

Полный отчёт: `docs/AI_BRAIN_VISUAL_VALIDATION.md`. Redacted production evidence собирается workflow `owner-e2e-audit.yml` без owner ID, токенов и приватных путей.

## Исправлено по итогам аудита

- добавлен read-only owner E2E exporter и workflow с проверкой observe-only/processing/SQLite/orphans;
- добавлен regression на точный переход Gallery history «Предыдущая»;
- исправлен ложный orphan: session `metadata.json` теперь считается referenced и не удаляется maintenance;
- после deploy повторный аудит подтвердил orphan private files 0.

Commercial candidate расширяет regression suite до 254 backend unittest tests и 49 site tests без реальных OpenAI image calls и без платежей.

## Historical migration v8 boundary (superseded by v9 product model above)

- migration v8 добавляет PaymentOrder/Attempt/Event/Webhook/Receipt/Audit и RefundIntent/Audit;
- Robokassa payment link и classic ResultURL доступны только за fail-closed flags;
- legacy paid callback был version-scoped; migration v9 теперь начисляет package +2/+1 без auto-unlock, а version выбирается позднее;
- MAX delivery failure сохраняет подтверждённую оплату и допускает повторную выдачу original;
- refund draft/CLI реализованы; provider execution требует Password3, operation key и отдельного enable;
- result и Gallery UX сокращены, удаление стало recoverable trash/restore;
- добавлены privacy-safe `pilot/payment/storage/backup/cleanup/cost/health` отчёты;
- каждый deploy принудительно возвращает payments/webhook/refunds off, provider disabled, sandbox и production approval false;
- owner sandbox Robokassa, публичный HTTPS ResultURL и настоящий платёж ещё не проверены.

## Не готово

Provider sandbox/real-payment evidence, operation-key refund reconciliation, окончательные legal/fiscal documents/operator details, support process, public deep link/site launch, внешний пилот 5 пользователей и 10/20-user evidence. Long polling допустим для малого allowlisted pilot, но не для сотен публичных пользователей. До платного публичного запуска нужен visual quality gate для identity/scene drift.

## 20.07.2026 — официальный сайт опубликован

- `site/public` — единственная реализация сайта; второй frontend не создаётся;
- MAX deep link проверен: `https://max.ru/se13572368_bot`;
- публичный продукт синхронизирован с backend: 49 ₽ за ещё два варианта и один выбранный original без watermark;
- добавлены оферта, privacy, согласие, правила, оплата/возврат и контакты;
- оплата и публичный запуск честно обозначены как недоступные в закрытом тестировании;
- CI проверяет mobile/desktop Lighthouse, ссылки, SEO, legal consistency, отсутствие секретов и atomic deploy;
- Google, Cloudflare и Quad9 возвращают `116.203.24.102` для apex и `www`; лишних AAAA-записей нет, NS — `ns1.reg.ru` и `ns2.reg.ru`;
- Let's Encrypt ECDSA-сертификат покрывает apex и `www`, OpenSSL verification и browser chain успешны, `certbot renew --dry-run` проходит;
- HTTP и `www` дают 301 на `https://pixoraai.ru`, финальный HTTPS включает CSP, HSTS и security headers;
- подтверждённые владельцем ИНН `631937938795` и e-mail `viner-89@mail.ru` опубликованы на главной, в контактах и legal-документах;
- публичные маршруты, 404, robots, sitemap, favicon, ссылки и W3C HTML validation проверены; цена везде 49 ₽, прежняя более высокая цена отсутствует.

Production HTTPS завершён на целевом VPS: Nginx 1.24 и Certbot 2.9 активны, immutable read-only release опубликован через `/opt/pixora-site/current`, 80/443 доступны, backend изолирован и health остаётся зелёным. После закрытия launch gate GitHub Actions repository variable `PIXORA_SITE_DEPLOY_ENABLED` установлена в `true`; одноимённая environment variable удалена, потому что job-level `if` вычисляется раньше Environment. Инженерный gate сайта для отправки на модерацию Robokassa закрыт. Sandbox, ResultURL, реальные платежи, pilot и пользовательские handlers не включались.

## 19.07.2026 — optional provider context

- официальный API-аудит Responses/image tool выполнен;
- migration v7 и optional provider context реализованы;
- source of truth не изменён: SQLite + Storage + GalleryVersion + SceneIntent;
- production flow остаётся `/v1/images/edits`;
- три OpenAI context flags по умолчанию и в deploy выключены;
- реальные OpenAI image requests для этой работы: 0;
- 208/208 локальных regression tests пройдены;
- controlled comparison на 4 calls только подготовлен и требует разрешения;
- визуальная польза, latency и стоимость пока не подтверждены.
