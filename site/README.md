# Ravuna AI website

`site/public` — единственный исходный каталог официального продуктового сайта
[`ravuna.ru`](https://ravuna.ru). Сайт статический: он не импортирует backend,
не читает SQLite, не содержит пользовательские фотографии или секреты и не
меняет runtime `photo-bot`.

## Публичный контракт

- бренд и публичное название продукта — **Ravuna**;
- проверенный MAX deep link — `https://max.ru/se13572368_bot`;
- цифровой продукт — «Пакет доступа Ravuna» за 49 ₽: две обработки и один
  оригинал без водяного знака, без подписки;
- оферта, политика конфиденциальности, согласие на обработку данных, правила,
  условия оплаты и возврата и контакты опубликованы на `ravuna.ru`;
- статические страницы возврата Robokassa ничего не начисляют и не подтверждают;
- SEO, Open Graph, Twitter Card, FAQ JSON-LD, manifest, robots и sitemap
  используют только `ravuna.ru`;
- все демонстрационные изображения синтетические; происхождение описано в
  `docs/VISUAL_ASSETS.md`.

Опубликованные реквизиты исполнителя: самозанятый Нурмухамитов Винер
Табризович, ИНН `631937938795`, Самара, `viner-89@mail.ru`.

## Локальная проверка

```powershell
python -m http.server 4173 --bind 127.0.0.1 --directory site/public
./site/scripts/smoke.ps1
python -m unittest discover -s site/tests -v

cd site
npm ci
New-Item -ItemType Directory -Force .lighthouse | Out-Null
npm run lighthouse
python scripts/assert_lighthouse.py .lighthouse/report-mobile.json
python scripts/assert_lighthouse.py .lighthouse/report-desktop.json
```

Порог Lighthouse для performance, accessibility, best practices и SEO — 95.
Сайт не выполняет runtime JavaScript.

## Production layout

```text
/opt/ravuna-site/releases/<commit-sha>
/opt/ravuna-site/current -> releases/<commit-sha>
/opt/ravuna-site/previous -> releases/<previous-sha>
/opt/ravuna-site/shared
```

Workflow `.github/workflows/site.yml` создаёт неизменяемый release, проверяет
public tree, атомарно переключает symlink и откатывает его при неуспешном smoke.
Deploy разрешён только repository variable `RAVUNA_SITE_DEPLOY_ENABLED=true`.

## Legacy payment compatibility

Неизменяемый ResultURL Robokassa продолжает работать на историческом
`pixoraai.ru`. Поэтому тот же Ravuna-branded статический артефакт публикуется в
legacy root `/opt/pixora-site`, а Nginx-конфигурация старого домена сохраняет
единственный proxy endpoint `/payments/robokassa/result`. Это не публичный бренд
и не второй сайт; менять MerchantLogin или ResultURL в рамках ребрендинга нельзя.

Операционные инструкции:

- `docs/SITE_PRODUCTION_RUNBOOK.md`;
- `docs/TLS_CERTIFICATE_RUNBOOK.md`;
- `docs/ROBOKASSA_SITE_MODERATION_CHECKLIST.md`;
- `site/scripts/rollback_remote.sh`.
