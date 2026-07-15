# Changelog

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
