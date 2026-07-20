# Current State

Актуально на 19.07.2026 для commercial MVP candidate. Production deployment обязан оставлять AI experiments и реальные платежи выключенными.

## Подтверждено в production

- deployed commit атомарно фиксируется workflow в `data/deployed_commit.txt`; фактический SHA проверяется post-deploy audit;
- `photo-bot.service` и MAX polling здоровы; `MAX_POLL_OBSERVE_ONLY=true`;
- owner allowlist настроен, pilot limit 0, пользовательские handlers выключены;
- OpenAI `gpt-image-2` — единственный production image provider; router/composite/segmentation выключены;
- SQLite migration v8, `PRAGMA quick_check=ok`, pending/processing attempts 0, processing dialogs 0, processing GalleryVersions 0 проверяются post-deploy audit;
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

Commercial candidate расширяет regression suite до 230 pytest tests и 26 subtests без реальных OpenAI image calls и без платежей.

## Commercial MVP candidate

- migration v8 добавляет PaymentOrder/Attempt/Event/Webhook/Receipt/Audit и RefundIntent/Audit;
- Robokassa payment link и classic ResultURL доступны только за fail-closed flags;
- paid callback разблокирует одну точную GalleryVersion; GalleryItem и соседние версии не разблокируются;
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
- публичная цена синхронизирована с backend: 49 ₽ за одну выбранную версию без watermark;
- добавлены оферта, privacy, согласие, правила, оплата/возврат и контакты;
- оплата и публичный запуск честно обозначены как недоступные в закрытом тестировании;
- CI проверяет mobile/desktop Lighthouse, ссылки, SEO, legal consistency, отсутствие секретов и atomic deploy;
- Google, Cloudflare и Quad9 возвращают `116.203.24.102` для apex и `www`; лишних AAAA-записей нет, NS — `ns1.reg.ru` и `ns2.reg.ru`;
- Let's Encrypt ECDSA-сертификат покрывает apex и `www`, OpenSSL verification и browser chain успешны, `certbot renew --dry-run` проходит;
- HTTP и `www` дают 301 на `https://pixoraai.ru`, финальный HTTPS включает CSP, HSTS и security headers;
- подтверждённые владельцем ИНН `631937938795` и e-mail `viner-89@mail.ru` опубликованы на главной, в контактах и legal-документах;
- публичные маршруты, 404, robots, sitemap, favicon, ссылки и W3C HTML validation проверены; цена везде 49 ₽, 149 ₽ отсутствует.

Production HTTPS завершён на целевом VPS: Nginx 1.24 и Certbot 2.9 активны, immutable read-only release опубликован через `/opt/pixora-site/current`, 80/443 доступны, backend изолирован и health остаётся зелёным. После закрытия launch gate GitHub Environment variable `PIXORA_SITE_DEPLOY_ENABLED` установлена в `true`. Инженерный gate сайта для отправки на модерацию Robokassa закрыт. Sandbox, ResultURL, реальные платежи, pilot и пользовательские handlers не включались.

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
