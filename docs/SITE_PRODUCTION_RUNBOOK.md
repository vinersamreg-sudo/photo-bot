# Pixora site production runbook

Сайт — отдельный статический artifact. Его корень `/opt/pixora-site/current` никогда не указывает на `/opt/photo-bot`, SQLite, пользовательское хранилище или `.env`.

## Целевой layout

```text
/opt/pixora-site/
  releases/<git-sha>/
  current -> releases/<git-sha>
  previous -> releases/<previous-sha>
  shared/acme/
  shared/rollback_remote.sh
  deployed_commit.txt
```

Release содержит только `site/public`. Symlink внутри release запрещён. Каталоги принадлежат deploy-пользователю `photoapp`; опубликованные release-файлы read-only.

## Baseline перед изменением

```bash
systemctl is-active photo-bot
cat /opt/photo-bot/data/deployed_commit.txt
grep -E '^(MAX_POLL_OBSERVE_ONLY|PAYMENTS_ENABLED|PAYMENT_WEBHOOK_ENABLED|ROBOKASSA_PRODUCTION_APPROVED)=' /opt/photo-bot/.env
nginx -t || true
ss -ltnp | grep -E ':(80|443) ' || true
ufw status numbered
```

Не выводить owner ID, токены и пароли.

## DNS cutover

Удалить старые записи, ведущие на `95.163.244.138`, и установить:

```text
pixoraai.ru      A      116.203.24.102
www.pixoraai.ru  A      116.203.24.102
```

Если существует `AAAA`, он также обязан вести на реально настроенный IPv6 целевого VPS; иначе удалить его. Дождаться одинакового результата у нескольких публичных resolver. TTL на время cutover рекомендуется 300 секунд.

## Bootstrap сервера

1. Установить Nginx и Certbot из репозитория Ubuntu.
2. Создать layout и передать его `photoapp:photoapp`.
3. Разместить проверенный artifact в `releases/<sha>`, записать SHA в `version.txt`, сделать release read-only и атомарно создать `current`.
4. Установить `site/nginx/pixoraai.ru-http-bootstrap.conf` как единственный site и выполнить `nginx -t`.
5. Разрешить UFW `80/tcp` и `443/tcp`, не изменяя SSH rule.
6. Проверить локально:

```bash
curl --fail -H 'Host: pixoraai.ru' http://127.0.0.1/
curl --fail -H 'Host: pixoraai.ru' http://127.0.0.1/legal/offer.html
```

## HTTPS

После DNS cutover выполнить шаги из `docs/TLS_CERTIFICATE_RUNBOOK.md`. HSTS появляется только в final HTTPS config.

## Включение CI deploy

`PIXORA_SITE_DEPLOY_ENABLED=true` разрешается установить как GitHub Actions repository variable только когда одновременно выполнены DNS, Nginx, TLS, external smoke и проверка всех документов. Job-level `if` вычисляется до подключения Environment `production`, поэтому environment variable здесь не работает как deploy gate. До установки repository variable workflow создаёт проверенный artifact, но не меняет production.

Deploy создаёт staging-каталог, отвергает symlink/secret-like файлы, переводит release в read-only, сохраняет `previous`, атомарно переключает `current` и откатывает его при HTTPS smoke failure.

## Smoke после deploy

```powershell
./site/scripts/smoke.ps1 https://pixoraai.ru
curl.exe -I https://pixoraai.ru/
curl.exe -I https://www.pixoraai.ru/
curl.exe https://pixoraai.ru/version.txt
```

Проверить 301 с HTTP и `www`, security headers, сертификат обоих имён, отсутствие `.env`, `.git`, `.map`, `.sqlite`, `.log`, `.bak`, directory listing и symlink escape.

## Rollback

На сервере от deploy-пользователя:

```bash
/opt/pixora-site/shared/rollback_remote.sh
```

Скрипт принимает только targets внутри `/opt/pixora-site/releases`, меняет `current`/`previous` атомарно и выполняет локальный HTTPS smoke. После rollback повторить внешний smoke.

## Проверка изоляции backend

После любого site deploy должны совпасть backend SHA и runtime-флаги baseline. `photo-bot` не перезапускается site workflow. Проверить один активный PID, healthcheck, observe-only и отключённые payment/provider/webhook/refund flags.
