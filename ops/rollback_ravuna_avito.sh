#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/ravuna_avito_current.sh"
ROOT=/opt/ravuna-avito
PREVIOUS_RELEASE=${1:-}
NGINX_BACKUP=${2:-}

if [ -n "$NGINX_BACKUP" ]; then
  test -d "$NGINX_BACKUP"
  sudo -n install -o root -g root -m 0644 "$NGINX_BACKUP/ravuna.ru" /etc/nginx/sites-available/ravuna.ru
  if [ -f "$NGINX_BACKUP/had-rate" ]; then
    sudo -n install -o root -g root -m 0644 "$NGINX_BACKUP/rate.conf" /etc/nginx/conf.d/ravuna-avito-rate.conf
  else
    sudo -n rm -f /etc/nginx/conf.d/ravuna-avito-rate.conf
  fi
  if [ -f "$NGINX_BACKUP/had-location" ]; then
    sudo -n install -o root -g root -m 0644 "$NGINX_BACKUP/location.conf" /etc/nginx/snippets/ravuna-avito-location.conf
  else
    sudo -n rm -f /etc/nginx/snippets/ravuna-avito-location.conf
  fi
  sudo -n nginx -t
  sudo -n systemctl reload nginx.service
fi

if [ -n "$PREVIOUS_RELEASE" ]; then
  PREVIOUS_RELEASE=$(readlink -e "$PREVIOUS_RELEASE")
  case "$PREVIOUS_RELEASE" in
    "$ROOT"/releases/*) ;;
    *) echo "previous release must be under $ROOT/releases" >&2; exit 2 ;;
  esac
  test -d "$PREVIOUS_RELEASE"
  ravuna_avito_set_current "$ROOT" "$PREVIOUS_RELEASE"
  sudo -n systemctl restart ravuna-avito-responder.service
  test "$(systemctl is-active ravuna-avito-responder.service)" = active
else
  sudo -n systemctl stop ravuna-avito-responder.service
  sudo -n systemctl disable ravuna-avito-responder.service
  ravuna_avito_remove_current "$ROOT"
fi

# Webhook unsubscription is an explicit Avito API/dashboard action performed
# before this script. Runtime secrets and SQLite are deliberately preserved.
echo "STATUS=ROLLED_BACK"
echo "PHOTO_BOT_TOUCHED=false"
