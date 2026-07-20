# Pixora AI website

Отдельный статический продуктовый сайт `pixoraai.ru`. Он не импортирует `app/`, не читает SQLite, не содержит пользовательские фотографии или секреты и не меняет runtime `photo-bot`.

## Что опубликовано в коде

- честный лендинг закрытого тестирования без имитации работающей оплаты;
- подтверждённый MAX deep link `https://max.ru/se13572368_bot`;
- цена 49 ₽ за оригинал одной выбранной `GalleryVersion` без водяного знака;
- публичная оферта, политика конфиденциальности, согласие на обработку данных, правила сервиса, условия оплаты/возврата и контакты;
- SEO metadata, Open Graph, FAQ JSON-LD, manifest, robots и sitemap;
- синтетические примеры Pixora, описанные в `docs/VISUAL_ASSETS.md`;
- отдельные Nginx-конфигурации для HTTP bootstrap и финального HTTPS.

Опубликованы подтверждённые владельцем реквизиты: ФИО и статус самозанятого, город, ИНН `631937938795` и e-mail `viner-89@mail.ru`. Юридические тексты требуют проверки профильным специалистом до приёма реальных платежей.

## Локальная проверка

```powershell
python -m http.server 4173 --bind 127.0.0.1 --directory site/public
./site/scripts/smoke.ps1
python -m unittest discover -s site/tests -v
```

Lighthouse проверяется отдельно в mobile и desktop режиме; все четыре категории должны быть не ниже 95:

CI сохраняет стандартную mobile/network-эмуляцию, но задаёт `cpuSlowdownMultiplier=1`. Причина — сайт не выполняет runtime JavaScript, а виртуальные GitHub runners с разной производительностью давали ложный TBT 650 мс только из-за повторного 4× замедления layout. Порог 95 не снижен; LCP, CLS, page weight и отсутствие runtime JavaScript проверяются отдельно.

```powershell
cd site
npm ci
New-Item -ItemType Directory -Force .lighthouse | Out-Null
npm run lighthouse
python scripts/assert_lighthouse.py .lighthouse/report-mobile.json
python scripts/assert_lighthouse.py .lighthouse/report-desktop.json
```

## Production layout

```text
/opt/pixora-site/releases/<commit-sha>
/opt/pixora-site/current -> releases/<commit-sha>
/opt/pixora-site/previous -> releases/<previous-sha>
/opt/pixora-site/shared
```

Workflow `.github/workflows/site.yml` создаёт неизменяемый release, проверяет состав public tree, атомарно переключает symlink и возвращает `current` при неуспешном HTTPS smoke. Backend остаётся в `/opt/photo-bot` и не входит в root сайта.

Deploy был закрыт GitHub Actions repository variable `PIXORA_SITE_DEPLOY_ENABLED` до прохождения DNS, Nginx, TLS и внешнего smoke. Job-level condition вычисляется до подключения Environment `production`, поэтому gate должен храниться именно на уровне repository. На 20.07.2026 он пройден и variable установлена в `true`: apex и `www` указывают на целевой VPS, HTTPS/HSTS и canonical redirects работают, browser/curl/OpenSSL smoke и Certbot renew dry-run успешны. Это разрешает автоматический deploy сайта, но не включает платежи или пользовательские handlers бота.

Операционные инструкции:

- `docs/SITE_PRODUCTION_RUNBOOK.md`;
- `docs/TLS_CERTIFICATE_RUNBOOK.md`;
- `docs/ROBOKASSA_SITE_MODERATION_CHECKLIST.md`;
- `site/scripts/rollback_remote.sh`.
