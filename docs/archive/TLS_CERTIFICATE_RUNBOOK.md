# TLS certificate runbook for ravuna.ru

## Certificate scope

The public certificate must cover:

- `ravuna.ru`;
- `www.ravuna.ru`.

The primary site redirects HTTP to HTTPS and `www` to the apex domain. HSTS is enabled only after both HTTPS names pass external verification.

## Issue or renew

```bash
sudo certbot certonly \
  --webroot \
  --webroot-path /opt/ravuna-site/shared/acme \
  -d ravuna.ru \
  -d www.ravuna.ru
sudo nginx -t
sudo systemctl reload nginx
sudo certbot renew --dry-run
```

Use a real owner email. Never place certificate-account credentials in Git.

## Verification

```bash
openssl s_client -connect ravuna.ru:443 -servername ravuna.ru </dev/null
openssl s_client -connect www.ravuna.ru:443 -servername www.ravuna.ru </dev/null
curl -I http://ravuna.ru/
curl -I http://www.ravuna.ru/
curl -I https://ravuna.ru/
curl -I https://www.ravuna.ru/
sudo certbot certificates
sudo nginx -t
```

Confirm trusted chain, SAN coverage, current validity window, 301 redirects, HTTP 200 on the apex HTTPS home page, no Nginx errors after reload and a successful external smoke.

The legacy certificate and vhost for the established Robokassa ResultURL are maintained separately. Do not remove or redirect the callback path while Robokassa still targets it.
