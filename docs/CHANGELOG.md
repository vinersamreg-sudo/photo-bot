# Changelog

## 2026-07-14 — Hetzner production migration

- подготовлен VPS `ai-prod-01` на Ubuntu 24.04;
- созданы раздельные admin/app пользователи и отдельный deploy key;
- включены SSH hardening, UFW и fail2ban;
- создан `/opt/photo-bot`, venv Python 3.12 и закрытый `.env`;
- CI/CD перенесён с REG.RU на Hetzner и rsync;
- добавлены secret scan, server-side unit tests и deployed SHA;
- документация актуализирована, неподтверждённый публичный бренд удалён;
- скомпрометированный OpenAI repository secret удалён и не переносился.

## До миграции

Минимальный production-каркас работал на shared hosting REG.RU. OpenAI API с его исходящего IP возвращал региональный `403 unsupported_country_region_territory`, что стало причиной миграции без использования обходных сетевых средств.
