# Changelog

## 20.07.2026 — Public DNS, TLS and Robokassa moderation site gate

- apex and `www` propagated to `116.203.24.102` across Google, Cloudflare and Quad9;
- Let's Encrypt ECDSA certificate issued for both names; OpenSSL/browser chain and renew dry-run passed;
- canonical HTTPS, `www` redirect, CSP/security headers and HSTS enabled after public smoke;
- owner-confirmed INN `631937938795` and e-mail `viner-89@mail.ru` published in site/legal source of truth;
- all public/legal routes, 404, links, W3C validation and exact 49 ₽ price passed; 149 ₽ is absent;
- `PIXORA_SITE_DEPLOY_ENABLED=true` enabled only after the full external launch gate passed;
- site engineering gate for Robokassa moderation is complete; payments, webhook, refunds, pilot and MAX handlers remain disabled;
- no real payment and no OpenAI image request performed.

## 19.07.2026 — Official Pixora website launch candidate

- existing static site upgraded without creating a competing frontend;
- verified MAX bot deep link and exact 49 ₽ one-version commercial boundary published;
- offer, privacy, personal-data consent, terms, payment/refund and seller contacts added;
- closed testing and disabled payment state disclosed without fake reviews or fake availability;
- SEO, FAQ JSON-LD, security headers, mobile layout and dual Lighthouse budgets hardened;
- atomic site release, rollback and HTTP bootstrap/HTTPS Nginx configs added;
- Robokassa official moderation checklist and site/TLS/legal runbooks added;
- no real payment and no OpenAI image request performed;
- public HTTPS remains blocked until owner changes apex and `www` DNS from the legacy IP to the Hetzner VPS and supplies confirmed INN/contact data.
- target VPS bootstrap completed: immutable read-only release, Nginx/Certbot, UFW 80/443, isolated public root and two direct-IP external smoke passes; HSTS/TLS and automatic site deploy remain off until DNS cutover.

## 19.07.2026 — Commercial MVP payment boundary

- migration v8 adds version-scoped payment orders, attempts, events, webhooks, receipts, audit and refunds;
- added Robokassa sandbox/production provider with fail-closed flags and classic ResultURL signature/idempotency checks;
- payment unlocks one exact GalleryVersion; delivery failure preserves paid state and supports re-delivery;
- refund prepare/submit/status CLI is gated by Password3, operation key and an independent enable flag;
- every deploy forces payments, webhook and refunds off, provider disabled, sandbox and production approval false;
- result/Gallery actions are shorter; delete is recoverable through trash/restore;
- added privacy-safe payment/pilot/storage/backup/cleanup/cost/health reports and commercial runbooks;
- regression suite is 230 pytest tests plus 26 subtests; no real OpenAI image request or money movement was performed in this sprint;
- Robokassa sandbox, public HTTPS ResultURL, legal/fiscal review and first real 49 RUB payment remain unverified gates.

## 17.07.2026 — Повторная owner-only visual validation

- выполнено ровно 5/5 разрешённых реальных `gpt-image-2` запросов через production MAX flow;
- подтверждены watermarked delivery, GalleryVersions 11–15, Correction, Repeat, History, Favorite и Current best;
- детерминированный parser теперь корректно передаёт `outfit.color=dark green`; correction резкости скал сработала;
- зафиксированы средняя provider duration 63.824 с и внутренний cost reserve 50 RUB;
- добавлен redacted read-only owner E2E audit workflow без идентификаторов, токенов и приватных путей;
- session `metadata.json` исключён из orphan cleanup; добавлены regression tests;
- production возвращён в `MAX_POLL_OBSERVE_ONLY=true`, pilot 0, processing 0, quick_check ok, orphans 0;
- visual drift композиции/identity остаётся риском; public launch readiness остаётся false.

## 17.07.2026 — Closed-pilot launch hardening

- `gpt-image-2` зафиксирован единственным production image provider v1; router выключен;
- `/start`, upload, processing, result и Gallery сокращены; «Идеи» разбиты по категориям;
- добавлены точные timeout/network/quota/policy/delivery/storage/size ошибки и restart recovery;
- migration v6 добавляет privacy-minimal product telemetry без prompt/photo/MAX ID;
- добавлены owner + 0/5/10/20 pilot allowlist stages;
- реализованы encrypted SQLite backup, restore test, off-site artifact и retention;
- добавлены dry-run/execute retention/temp/orphan cleanup и `launch-status`;
- сайт приведён к фактическому пути и ограничениям качества/оплаты;
- regression suite расширен до 153 pytest checks, 145 unittest checks и 26 subtests без реальных OpenAI image calls.
- production commit `7deb6c0`, migration v6, observe-only runtime and encrypted backup/restore/off-site workflow подтверждены; operational readiness зелёный, public readiness остаётся false.

## 16.07.2026 — Direct photo flow and AI Brain 2.0

- основной MAX flow сокращён до `/start → photo → text → result`;
- отдельный legal/Continue экран, главное меню, «Своя идея» и prompt confirmation удалены из основного пути;
- implicit consent и legal document versions записываются только после валидного сохранения source;
- возвращающийся пользователь продолжает с допустимым сохранённым фото без повторной загрузки;
- готовые сценарии перенесены в отдельный каталог «✨ Идеи» и могут запускаться без дополнительного prompt;
- следующий текст после результата автоматически становится correction текущей версии;
- EditPlan schema v2 получила provider-neutral scene fields для background/lighting/camera/outfit/pose/objects/negative;
- Correction меняет только затронутые scene fields, Repeat сохраняет scene и branch;
- raw Russian text исключён из provider prompt, а provider boundary блокирует non-ASCII до API;
- SQLite schema, Storage, Gallery, quota, payments, watermark, security и delivery boundary не менялись.

## 16.07.2026 — Structured AI Brain

- добавлены `app/edit_intent.py` и `app/prompt_builder.py`;
- correction image input переключён с исходного source на original выбранной успешной версии;
- repeat сохраняет effective intent и исходный input branch;
- migration v4 хранит EditPlan/provider prompt/source version и backfill legacy;
- provider quality/size/fidelity/output/retries вынесены в конфигурацию, demo default повышен с low до medium;
- добавлены необязательные 👍/👎 и технические агрегаты без пользовательского free text;
- добавлен admin-safe `ai-inspect`;
- добавлены regression tests на скалы, blur, отрицания, multiple corrections, lineage и delivery boundary.

## 2026-07-15 — owner-only application gate

- добавлен обязательный `MAX_OWNER_USER_IDS` allowlist перед включением handlers;
- посторонние пользователи получают вежливый ответ о закрытом тестировании без создания диалога и OpenAI-вызова;
- GitHub deploy получает owner ID только из Environment secret и fail-closed возвращается в observe-only при его отсутствии;
- добавлены тесты owner/non-owner, callback, missing allowlist и deploy policy.

## 2026-07-15 — owner-only MAX production polling

- подтверждены прошедший модерацию бот `Pixora обработка фото ИИ` и наличие `MAX_BOT_TOKEN` в GitHub Environment `production`;
- добавлен transport-only `MAX_POLL_OBSERVE_ONLY=true`: runtime получает обновления и marker, но не вызывает пользовательские handlers, не отправляет сообщения и не запускает OpenAI image generation;
- MAX transport errors классифицируются как configuration/auth/forbidden/inactive/network/timeout/rate-limit/duplicate-instance без раскрытия ответа или токена;
- для `platform-api2.max.ru` добавлен официальный `Russian Trusted Root CA` из `gu-st.ru`; он применяется только к MAX API client с полной TLS-проверкой, DER SHA-256 закреплён тестом;
- production healthcheck проверяет SQLite, свежесть связи с MAX, active systemd MainPID, команду процесса и удерживаемый single-instance lock;
- deploy передаёт MAX token через stdin, устанавливает hardened systemd unit, проверяет один PID, duplicate start, graceful restart и отсутствие значений секретов в runtime log;
- существующий runtime log приведён к правам `600`; новые файлы создаются с systemd `UMask=0077`;
- прямой background-start выведен из эксплуатации; публичный webhook, пользовательский `/start`, изображения, callbacks, оплата и сайт остаются вне этапа.

## 2026-07-15 — Pixora AI product landing

- добавлен изолированный static landing `pixoraai.ru` без изменений backend runtime;
- реализованы responsive Hero, сценарии, собственные synthetic before/after, benefits, steps, FAQ, legal placeholders и MAX CTA;
- добавлены SEO/schema/robots/sitemap, WebP responsive assets и accessible reduced-motion интерфейс;
- добавлены structural tests, HTTP smoke, Lighthouse budget ≥95 и отдельный opt-in atomic Hetzner deploy;
- подготовлены Hero-варианты, brand/color guidance, provenance изображений и roadmap сайта.

## 2026-07-15 — MAX transport and fake end-to-end slice

- аудит не обнаружил отдельного старого MAX-кода/репозитория или production token;
- зафиксирован официальный `platform-api2.max.ru` contract и решение о thin transport в одном Python-приложении;
- добавлены MAX HTTP/media/callback client, SQLite state/legal/idempotency migration v3 и single-instance polling smoke runner;
- реализованы `/start` → legal → custom/upload → confirmation → preview → correction/repeat → works → delete;
- original не отправляется, quota меняется только после успешного preview delivery, delete немедленно очищает файлы;
- deploy применяет SQLite migrations до выбора runtime mode, поэтому disabled transport не оставляет production schema устаревшей;
- live MAX smoke, Webhook, systemd install и payment не заявляются готовыми из-за отсутствующих credentials/domain.

## 2026-07-15 — personal AI studio domain

- добавлены одна Gallery на пользователя, работы, неизменяемые версии и version-specific originals;
- repeat/correction сохраняют parent/effective prompt и создают новую версию той же работы;
- добавлены favorites/current best, collections, tags, preferences, search, recent/continue, rating и rename;
- добавлены soft delete/restore, dry-run cleanup и retention 30 дней для demo / 180 дней для paid;
- migration v2 backfill связывает существующие demo-данные без перемещения production-файлов;
- реальные MAX-экраны, slider, интерактивный поиск и экспорт оставлены следующему этапу.

## 15.07.2026 — guarded demo MVP foundation

- принята механика «сначала фактический watermarked результат, затем оплата конкретного original»;
- добавлены SQLite demo-сессии, attempts, legal consents и payment intents;
- реализованы одна фотография, пять успешно доставленных результатов, TTL, idempotency, cooldown, concurrency и дневные бюджеты;
- добавлены private filesystem storage, кириллический watermark, OpenAI/fake provider и cost telemetry;
- добавлены CLI `demo-edit`/`demo-stats` и транспорт-независимый MAX UX adapter;
- реальный MAX transport, эквайринг и юридическая экспертиза остаются незавершёнными; публичный бренд не утверждён.
- один ограниченный production smoke на синтетическом портрете успешно прошёл через тот же CLI/OpenAI provider: original не раскрыт, preview защищён watermark, двойного списания квоты нет.

## 2026-07-15 — MAX competitor mystery shopping

- в отдельном researcher-проекте пройдены безопасные пользовательские пути восьми выбранных конкурентов без платежей и личных фото;
- подтверждены четыре бесплатных результата, реальные paywall/балансы/цены, обязательные подписки и один broken-flow;
- в стратегию перенесены требования: цена/срок/хранение до списания, сохранение состояния, correction, автоматический технический возврат и удаление;
- добавлены ADR-009/010 и ссылка на `DEEP_COMPETITOR_RESEARCH.md` / `PRODUCT_BLUEPRINT_FOR_PHOTO_BOT.md` в отдельном проекте;
- публичный бренд и финальные тарифы по-прежнему не утверждены.

## 2026-07-14 — MAX public market research

- в отдельном локальном проекте собрана широкая выборка публично обнаруживаемых MAX-каналов и ботов по 31 запросу;
- подтверждены 112 уникальных публичных объектов и 44 релевантных ИИ-фото конкурента;
- в продуктовую стратегию перенесены только выводы: сценарное позиционирование, минимальное меню, приватность и прозрачность цены;
- исследование не утверждает публичный бренд, цену запуска или оценку выручки конкурентов;
- источник: `C:\Users\viner\OneDrive\Документы\ИИ Фотошоп\research`.

## 2026-07-14 — Hetzner production migration

- подготовлен VPS `ai-prod-01` на Ubuntu 24.04;
- созданы раздельные admin/app пользователи и отдельный deploy key;
- включены SSH hardening, UFW и fail2ban;
- создан `/opt/photo-bot`, venv Python 3.12 и закрытый `.env`;
- CI/CD перенесён с REG.RU на Hetzner и rsync;
- добавлены secret scan, server-side unit tests и deployed SHA;
- документация актуализирована, неподтверждённый публичный бренд удалён;
- скомпрометированный OpenAI repository secret удалён и не переносился.
- первый Hetzner deploy коммита `d80f7e0` успешно проверен в GitHub Actions;
- после успешного deploy удалены четыре устаревших REG.RU repository secrets.

## До миграции

Минимальный production-каркас работал на shared hosting REG.RU. OpenAI API с его исходящего IP возвращал региональный `403 unsupported_country_region_territory`, что стало причиной миграции без использования обходных сетевых средств.

## 16.07.2026 — hybrid processing foundation

- пять typed modes и deterministic router;
- license-aware catalog и fail-closed asset verification;
- local Pillow enhancement/composite без external image calls;
- optional rembg CPU с pinned model checksum, auto-download запрещён;
- migration v5 сохраняет mode/provider/pipeline/mask/asset lineage;
- corrections/repeat наследуют parent plan; failures не создают version и quota;
- 131 tests, license audit и offline benchmark; OpenAI image requests: 0;
- production flags закрыты до assets/model/VPS/visual gates.

## 19.07.2026 — optional OpenAI provider context

- официальный audit Responses/Conversation/Image APIs;
- migration v7: provider contexts, per-version response lineage и telemetry;
- Responses image adapter с pinned `gpt-image-2` и parent `previous_response_id`;
- stateless fallback, branch isolation, depth/idle reset и cleanup tombstones;
- CLI `provider-context-cleanup` и retention integration;
- deploy принудительно оставляет три context flags выключенными;
- 208/208 unittest tests, dependency check, secret scan, license audit и compile check;
- comparison подготовлен, не запускался; реальные image requests: 0.
