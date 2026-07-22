#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "deploy_nginx_resulturl.sh must run as root" >&2
  exit 1
fi

CANDIDATE="${1:-}"
ACTIVE=/etc/nginx/sites-available/pixoraai.ru
ENABLED=/etc/nginx/sites-enabled/pixoraai.ru
BACKUP=$(mktemp /tmp/pixoraai.ru.conf.backup.XXXXXX)

if [ -z "$CANDIDATE" ] || [ ! -f "$CANDIDATE" ] || [ -L "$CANDIDATE" ]; then
  echo "A regular Nginx candidate file is required" >&2
  rm -f "$BACKUP"
  exit 1
fi
if [ ! -f "$ACTIVE" ] || [ "$(readlink -f "$ENABLED")" != "$ACTIVE" ]; then
  echo "Unexpected Pixora Nginx layout" >&2
  rm -f "$BACKUP"
  exit 1
fi

cp "$ACTIVE" "$BACKUP"
rollback() {
  install -o root -g root -m 644 "$BACKUP" "$ACTIVE"
  nginx -t >/dev/null
  systemctl reload nginx
  rm -f "$BACKUP"
}
trap rollback ERR

install -o root -g root -m 644 "$CANDIDATE" "$ACTIVE"
nginx -t
systemctl reload nginx

GET_STATUS=000
# Nginx reloads gracefully; an immediate request may still reach an old worker.
# Wait until the exact location is served by the new generation before probing POST.
for _attempt in $(seq 1 10); do
  GET_STATUS=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --resolve pixoraai.ru:443:127.0.0.1 \
    https://pixoraai.ru/payments/robokassa/result)
  if [ "$GET_STATUS" = 405 ]; then
    break
  fi
  sleep 1
done
POST_STATUS=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --resolve pixoraai.ru:443:127.0.0.1 \
  --request POST --data 'readiness_probe=1' \
  https://pixoraai.ru/payments/robokassa/result)
printf 'candidate_resulturl_get_status=%s\n' "$GET_STATUS"
printf 'candidate_resulturl_post_disabled_status=%s\n' "$POST_STATUS"
test "$GET_STATUS" = 405
test "$POST_STATUS" = 503

trap - ERR
rm -f "$BACKUP"
printf 'resulturl_get_status=%s\n' "$GET_STATUS"
printf 'resulturl_post_disabled_status=%s\n' "$POST_STATUS"
