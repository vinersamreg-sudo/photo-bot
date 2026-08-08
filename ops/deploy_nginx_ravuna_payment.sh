#!/usr/bin/env bash
set -euo pipefail

CANDIDATE="${1:-}"
if [ "$(id -u)" -ne 0 ]; then
  echo "deploy_nginx_ravuna_payment.sh must run as root" >&2
  exit 1
fi

ACTIVE=/etc/nginx/sites-available/ravuna.ru
ENABLED=/etc/nginx/sites-enabled/ravuna.ru
BACKUP=$(mktemp /tmp/ravuna.ru.conf.backup.XXXXXX)

cleanup() { rm -f "$BACKUP"; }
if [ -z "$CANDIDATE" ] || [ ! -f "$CANDIDATE" ] || [ -L "$CANDIDATE" ]; then
  echo "A regular Ravuna Nginx candidate file is required" >&2
  cleanup
  exit 1
fi
if [ ! -f "$ACTIVE" ] || [ "$(readlink -f "$ENABLED")" != "$ACTIVE" ]; then
  echo "Unexpected Ravuna Nginx layout" >&2
  cleanup
  exit 1
fi
grep -F 'proxy_pass http://127.0.0.1:8091$request_uri;' "$CANDIDATE" >/dev/null
grep -F '^/(?:p|payment/(?:success|fail))/[0-9a-f]{32}/?$' "$CANDIDATE" >/dev/null

cp "$ACTIVE" "$BACKUP"
rollback() {
  install -o root -g root -m 644 "$BACKUP" "$ACTIVE"
  nginx -t >/dev/null
  systemctl reload nginx
  cleanup
}
trap rollback ERR

install -o root -g root -m 644 "$CANDIDATE" "$ACTIVE"
nginx -t
systemctl reload nginx

HEADERS=$(mktemp /tmp/ravuna-payment-headers.XXXXXX)
trap 'rm -f "$HEADERS"; rollback' ERR
STATUS=000
for _attempt in $(seq 1 10); do
  STATUS=$(curl --silent --show-error --output /dev/null --dump-header "$HEADERS" \
    --write-out '%{http_code}' --resolve ravuna.ru:443:127.0.0.1 \
    https://ravuna.ru/p/00000000000000000000000000000000)
  if [ "$STATUS" = 404 ] && grep -Fi 'RavunaPaymentWebhook' "$HEADERS" >/dev/null; then
    break
  fi
  sleep 1
done
test "$STATUS" = 404
grep -Fi 'RavunaPaymentWebhook' "$HEADERS" >/dev/null

trap - ERR
rm -f "$HEADERS"
cleanup
printf 'ravuna_payment_routes_status=%s\n' "$STATUS"
