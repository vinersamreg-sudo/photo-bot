# TLS certificate runbook for pixoraai.ru

## RCA ошибки `ERR_SSL_VERSION_OR_CIPHER_MISMATCH`

На 19.07.2026 `pixoraai.ru` и `www.pixoraai.ru` резолвились в `95.163.244.138`, тогда как production Pixora находится на `116.203.24.102`. Старый адрес отвечал по HTTP через OpenResty, а TLS handshake завершался fatal alert. На целевом VPS Nginx, Certbot и listeners 80/443 отсутствовали. Ошибка вызвана неверным DNS/чужим TLS-контуром, а не набором cipher на Pixora VPS.

## Условия выпуска

- A-записи обоих имён указывают на `116.203.24.102` у авторитетного DNS и публичных resolver;
- возможные AAAA-записи либо корректны, либо удалены;
- `/.well-known/acme-challenge/` доступен снаружи по HTTP;
- UFW разрешает 80 и 443;
- HTTP bootstrap проходит `nginx -t`.

## Выпуск

```bash
sudo certbot certonly --webroot \
  --webroot-path /opt/pixora-site/shared/acme \
  -d pixoraai.ru -d www.pixoraai.ru \
  --agree-tos --no-eff-email -m <verified-owner-email>
sudo install -m 644 site/nginx/pixoraai.ru.conf /etc/nginx/sites-available/pixoraai.ru
sudo ln -sfn /etc/nginx/sites-available/pixoraai.ru /etc/nginx/sites-enabled/pixoraai.ru
sudo nginx -t
sudo systemctl reload nginx
```

Использовать только реально работающий e-mail владельца. Не подставлять `hello@pixoraai.ru`, пока доставка почты не подтверждена.

## Проверка

```bash
openssl s_client -connect pixoraai.ru:443 -servername pixoraai.ru </dev/null
openssl s_client -connect pixoraai.ru:443 -servername www.pixoraai.ru </dev/null
curl -I http://pixoraai.ru/
curl -I https://pixoraai.ru/
curl -I https://www.pixoraai.ru/
```

Проверить hostname/SAN для обоих имён, доверенную цепочку, TLS 1.2/1.3, HTTP→HTTPS и `www`→apex, HSTS и отсутствие mixed content.

## Renewal

```bash
sudo systemctl enable --now certbot.timer
sudo certbot renew --dry-run
systemctl list-timers certbot.timer
```

После dry-run убедиться, что hook reload Nginx срабатывает и `nginx -t` остаётся успешным. Certbot dry-run нельзя считать выполненным до реального сертификата и корректного DNS.

## Запреты

- не выпускать сертификат только для apex без `www`;
- не включать HSTS на HTTP-only bootstrap;
- не использовать `--insecure` как production smoke;
- не перенаправлять ACME challenge на MAX или backend;
- не утверждать, что сертификат готов, только потому что Nginx config написан.
