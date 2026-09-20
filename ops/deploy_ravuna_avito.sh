#!/usr/bin/env bash
set -Eeuo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: deploy_ravuna_avito.sh STAGE_ROOT TARGET_SHA" >&2
  exit 2
fi

STAGE_ROOT=$1
TARGET_SHA=$2
ROOT=/opt/ravuna-avito
RELEASES=$ROOT/releases
FINAL_RELEASE=$RELEASES/$TARGET_SHA
TEMP_RELEASE=$RELEASES/.${TARGET_SHA}.tmp
ENV_FILE=$ROOT/.env
ENV_CANDIDATE=$STAGE_ROOT/avito.env
NGINX_SITE=/etc/nginx/sites-available/ravuna.ru
NGINX_RATE=/etc/nginx/conf.d/ravuna-avito-rate.conf
NGINX_LOCATION=/etc/nginx/snippets/ravuna-avito-location.conf
BACKUP_ROOT=$ROOT/nginx-backups/previous

[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$(id -un)" = photoapp
test -f "$STAGE_ROOT/release.tar.gz"
test -f "$STAGE_ROOT/manifest.json"
test -f "$ENV_CANDIDATE"

umask 077
sudo -n install -d -o photoapp -g photoapp -m 0755 "$ROOT" "$RELEASES"
sudo -n install -d -o photoapp -g photoapp -m 0700 "$ROOT/data"
install -d -m 0700 "$BACKUP_ROOT"

OLD_CURRENT=$(readlink -f "$ROOT/current" 2>/dev/null || true)
OLD_SERVICE_ACTIVE=$(systemctl is-active ravuna-avito-responder.service 2>/dev/null || true)
PHOTO_PID=$(systemctl show photo-bot.service --property=MainPID --value)
PHOTO_RESTARTS=$(systemctl show photo-bot.service --property=NRestarts --value)
ENV_BEFORE=""
if [ -f "$ENV_FILE" ]; then
  ENV_BEFORE=$(sha256sum "$ENV_FILE" | awk '{print $1}')
fi

rm -f "$BACKUP_ROOT"/*
sudo -n install -o photoapp -g photoapp -m 0600 "$NGINX_SITE" "$BACKUP_ROOT/ravuna.ru"
if [ -f "$NGINX_RATE" ]; then
  sudo -n install -o photoapp -g photoapp -m 0600 "$NGINX_RATE" "$BACKUP_ROOT/rate.conf"
  : > "$BACKUP_ROOT/had-rate"
fi
if [ -f "$NGINX_LOCATION" ]; then
  sudo -n install -o photoapp -g photoapp -m 0600 "$NGINX_LOCATION" "$BACKUP_ROOT/location.conf"
  : > "$BACKUP_ROOT/had-location"
fi

SWITCHED=0
NGINX_CHANGED=0
rollback() {
  status=$?
  trap - EXIT
  rm -f "$ENV_CANDIDATE"
  if [ "$status" -eq 0 ]; then return; fi
  set +e
  if [ "$NGINX_CHANGED" -eq 1 ]; then
    sudo -n install -o root -g root -m 0644 "$BACKUP_ROOT/ravuna.ru" "$NGINX_SITE"
    if [ -f "$BACKUP_ROOT/had-rate" ]; then sudo -n install -o root -g root -m 0644 "$BACKUP_ROOT/rate.conf" "$NGINX_RATE"; else sudo -n rm -f "$NGINX_RATE"; fi
    if [ -f "$BACKUP_ROOT/had-location" ]; then sudo -n install -o root -g root -m 0644 "$BACKUP_ROOT/location.conf" "$NGINX_LOCATION"; else sudo -n rm -f "$NGINX_LOCATION"; fi
    sudo -n nginx -t && sudo -n systemctl reload nginx.service
  fi
  if [ "$SWITCHED" -eq 1 ]; then
    if [ -n "$OLD_CURRENT" ] && [ -d "$OLD_CURRENT" ]; then
      ln -sfn "$OLD_CURRENT" "$ROOT/current.rollback"
      mv -Tf "$ROOT/current.rollback" "$ROOT/current"
      if [ "$OLD_SERVICE_ACTIVE" = active ]; then
        sudo -n systemctl restart ravuna-avito-responder.service
      else
        sudo -n systemctl stop ravuna-avito-responder.service
      fi
    else
      sudo -n systemctl stop ravuna-avito-responder.service
      sudo -n systemctl disable ravuna-avito-responder.service
    fi
  fi
  echo "Avito-only deploy rolled back; .env and SQLite were preserved" >&2
  exit "$status"
}
trap rollback EXIT

rm -rf -- "$TEMP_RELEASE"
install -d -m 0755 "$TEMP_RELEASE"
tar -xzf "$STAGE_ROOT/release.tar.gz" -C "$TEMP_RELEASE"
python3 "$TEMP_RELEASE/scripts/build_deploy_artifact.py" \
  --archive "$STAGE_ROOT/release.tar.gz" \
  --manifest "$STAGE_ROOT/manifest.json" \
  --verify-directory "$TEMP_RELEASE"
printf '%s\n' "$TARGET_SHA" > "$TEMP_RELEASE/.deploy-sha"

python3 -m venv "$TEMP_RELEASE/venv"
"$TEMP_RELEASE/venv/bin/pip" install --disable-pip-version-check -q -r "$TEMP_RELEASE/requirements.txt"
"$TEMP_RELEASE/venv/bin/python" -m compileall -q "$TEMP_RELEASE/app/avito_responder"
PYTHONPATH="$TEMP_RELEASE" "$TEMP_RELEASE/venv/bin/python" - <<'PY'
from app.avito_responder.config import AvitoResponderSettings
from app.avito_responder.repository import AvitoRepository
assert AvitoResponderSettings
assert AvitoRepository
PY

if [ -e "$FINAL_RELEASE" ]; then
  test "$(cat "$FINAL_RELEASE/.deploy-sha")" = "$TARGET_SHA"
  rm -rf -- "$TEMP_RELEASE"
else
  mv "$TEMP_RELEASE" "$FINAL_RELEASE"
fi
find "$FINAL_RELEASE" -type d -exec chmod 0755 {} +

if [ ! -e "$ENV_FILE" ]; then
  install -m 0600 "$ENV_CANDIDATE" "$ENV_FILE"
fi
rm -f "$ENV_CANDIDATE"
test ! -L "$ENV_FILE"
test "$(stat -c '%a' "$ENV_FILE")" = 600
test "$(stat -c '%U:%G' "$ENV_FILE")" = photoapp:photoapp
grep -Eq '^AVITO_AUTO_REPLY_ENABLED="?false"?$' "$ENV_FILE"
grep -Eq '^AVITO_ALLOWED_ITEM_IDS="?8191967914"?$' "$ENV_FILE"

ln -sfn "$FINAL_RELEASE" "$ROOT/current.new"
mv -Tf "$ROOT/current.new" "$ROOT/current"
SWITCHED=1

sudo -n install -o root -g root -m 0644 \
  "$ROOT/current/ops/ravuna-avito-responder.service" \
  /etc/systemd/system/ravuna-avito-responder.service
sudo -n install -o root -g root -m 0644 \
  "$ROOT/current/ops/nginx/ravuna-avito-rate.conf" "$NGINX_RATE"
sudo -n install -o root -g root -m 0644 \
  "$ROOT/current/ops/nginx/ravuna-avito-location.conf" "$NGINX_LOCATION"
sudo -n install -o root -g root -m 0644 \
  "$ROOT/current/site/nginx/ravuna.ru.conf" "$NGINX_SITE"
NGINX_CHANGED=1
sudo -n nginx -t

sudo -n systemctl daemon-reload
sudo -n systemctl enable ravuna-avito-responder.service
sudo -n systemctl restart ravuna-avito-responder.service
test "$(systemctl is-active ravuna-avito-responder.service)" = active
test "$(systemctl show ravuna-avito-responder.service --property=NRestarts --value)" = 0

STATUS=$("$ROOT/current/venv/bin/python" "$ROOT/current/ops/run_ravuna_avito_env.py" \
  "$ENV_FILE" -- "$ROOT/current/venv/bin/python" -m app.avito_responder.cli status)
python3 - "$STATUS" <<'PY'
import json, sys
status = json.loads(sys.argv[1])
assert status["enabled"] is False
assert status["allowed_item_count"] == 1
assert status["debounce_seconds"] == 8
assert status["model"] == "gpt-5.4-mini"
PY
DRY_RUN=$("$ROOT/current/venv/bin/python" "$ROOT/current/ops/run_ravuna_avito_env.py" \
  "$ENV_FILE" -- "$ROOT/current/venv/bin/python" -m app.avito_responder.cli dry-run \
  --fixture "$ROOT/current/tests/fixtures/avito_first_reply.json")
python3 - "$DRY_RUN" <<'PY'
import json, sys
result = json.loads(sys.argv[1])
assert result["status"] == "dry_run"
assert result["sent"] is False
assert result["model"] == "gpt-5.4-mini"
assert result["reply"]
PY
test -f "$ROOT/data/avito_responder.sqlite3"
test "$(stat -c '%a' "$ROOT/data")" = 700
test "$(stat -c '%U:%G' "$ROOT/data")" = photoapp:photoapp
test "$(stat -c '%a' "$ROOT/data/avito_responder.sqlite3")" = 600

sudo -n systemctl reload nginx.service
"$ROOT/current/venv/bin/python" "$ROOT/current/ops/probe_ravuna_avito.py" "$ENV_FILE"

test "$(systemctl show photo-bot.service --property=MainPID --value)" = "$PHOTO_PID"
test "$(systemctl show photo-bot.service --property=NRestarts --value)" = "$PHOTO_RESTARTS"
if [ -n "$ENV_BEFORE" ]; then
  test "$(sha256sum "$ENV_FILE" | awk '{print $1}')" = "$ENV_BEFORE"
fi

trap - EXIT
echo "STATUS=PASS"
echo "DEPLOYED_SHA=$TARGET_SHA"
echo "AUTO_REPLY_ENABLED=false"
echo "PHOTO_BOT_UNCHANGED=true"
