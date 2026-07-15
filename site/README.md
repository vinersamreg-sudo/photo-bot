# Pixora AI website

Отдельный статический продуктовый лендинг `pixoraai.ru`. Сайт не импортирует `app/`, не обращается к SQLite и не меняет runtime `photo-bot`.

## Локальный запуск

```powershell
python -m http.server 4173 --bind 127.0.0.1 --directory site/public
./site/scripts/smoke.ps1
python -m unittest discover -s site/tests -v
```

Lighthouse:

```powershell
cd site
npm ci
New-Item -ItemType Directory -Force .lighthouse | Out-Null
npm run lighthouse
python scripts/assert_lighthouse.py .lighthouse/report.json
```

Порог CI — не ниже 95 для Performance, Accessibility, Best Practices и SEO. Внешних шрифтов, аналитики и runtime-зависимостей нет. Raster-изображения — собственные синтетические материалы Pixora в WebP; их происхождение описано в `docs/VISUAL_ASSETS.md`.

## MAX CTA

Все CTA сейчас используют временный `https://max.ru/`. Перед публичным запуском нужно одной заменой установить подтверждённую ссылку бота и обновить тест `test_max_links_are_safe_placeholders_and_trackable`. Атрибут `data-max-cta` готов для privacy-friendly аналитики кликов; сам сайт ничего не отправляет наружу.

## CI и deploy

`.github/workflows/site.yml` всегда запускает structural tests, HTTP smoke, Lighthouse и публикует статический artifact. Deploy отделён от backend workflow и выполняется только при GitHub Environment variable `PIXORA_SITE_DEPLOY_ENABLED=true`.

Он использует существующие secret names `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_PORT`, `HETZNER_SSH_PRIVATE_KEY`, создаёт immutable release в `/opt/pixora-site/releases/<sha>` и атомарно меняет symlink `/opt/pixora-site/current`.

До включения deploy администратор должен:

1. направить DNS `A/AAAA` для `pixoraai.ru` и `www.pixoraai.ru` на Hetzner;
2. создать `/opt/pixora-site/releases` и передать владение пользователю deploy (`photoapp`);
3. установить `site/nginx/pixoraai.ru.conf` в Nginx и проверить `nginx -t`;
4. открыть HTTP/HTTPS без изменения SSH-доступа;
5. получить TLS certificate и включить redirect HTTP → HTTPS;
6. выполнить `./site/scripts/smoke.ps1 https://pixoraai.ru`;
7. заменить временную ссылку MAX и только после legal review разрешить индексацию документов.

Административные действия не автоматизированы: домен, TLS и ownership требуют явного подтверждения владельца. Backend остаётся в `/opt/photo-bot` и не используется Nginx-конфигурацией сайта.

## Перед публичным запуском

- утвердить юридические тексты и реквизиты оператора;
- подтвердить адрес `hello@pixoraai.ru`;
- заменить MAX placeholder на deep link бота;
- получить согласие на использование любых будущих пользовательских примеров;
- подключать аналитику только после решения о consent/cookie policy.
