# Changelog

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
