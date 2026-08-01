# Ravuna site production runbook

The public site is a static artifact isolated from the backend. Its primary release root is `/opt/ravuna-site/current`; it never points to `/opt/photo-bot`, SQLite, user storage or `.env`.

## Release layout

```text
/opt/ravuna-site/
  current -> releases/<sha>
  previous -> releases/<sha>
  releases/<sha>/
  shared/acme/
  shared/rollback_remote.sh
```

GitHub Actions builds and validates `site/public`, uploads an immutable artifact and atomically switches `current`. `site/public` is the only active source of public copy, SEO, legal pages and payment-return pages.

## DNS

```text
ravuna.ru      A      116.203.24.102
www.ravuna.ru  A      116.203.24.102
```

Before certificate or Nginx changes, verify Google, Cloudflare and Quad9. Do not rely only on the VPS resolver.

## Deploy gate

The repository variable `RAVUNA_SITE_DEPLOY_ENABLED=true` permits the deploy job only after DNS, Nginx, trusted TLS, external smoke and legal-page checks pass. The workflow always validates the artifact even when deploy is gated.

## External smoke

```powershell
./site/scripts/smoke.ps1 https://ravuna.ru
curl.exe -I https://ravuna.ru/
curl.exe -I https://www.ravuna.ru/
curl.exe https://ravuna.ru/version.txt
```

Verify the home page, contacts, offer, privacy, personal-data, payment/refund, terms, success and failure pages, robots, sitemap, manifest, favicon and an expected 404. Search the delivered HTML for the previous public brand and obsolete 149 ₽ price.

## Rollback

```bash
/opt/ravuna-site/shared/rollback_remote.sh
```

The script accepts only targets inside `/opt/ravuna-site/releases`, switches symlinks atomically and performs a local HTTPS smoke. Repeat the external smoke after rollback.

## Legacy payment callback compatibility

`https://pixoraai.ru/payments/robokassa/result` remains an immutable Robokassa ResultURL and is still served by the legacy Nginx vhost. The old static root `/opt/pixora-site` may mirror the same Ravuna artifact so browser return pages remain safe during external-cabinet migration. This compatibility layer must not reintroduce the previous public brand into active content.
