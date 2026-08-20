#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Content Studio deployment requires root" >&2
  exit 1
fi
if [ "$#" -ne 2 ]; then
  echo "usage: deploy_ravuna_content_studio.sh <release.tar.gz> <git-sha>" >&2
  exit 2
fi

ARCHIVE=$1
REVISION=$2
ROOT=/opt/ravuna-content
RELEASES=$ROOT/releases
RELEASE=$RELEASES/$REVISION
VENV=$ROOT/venv
LOCK=/run/lock/ravuna-content-deploy.lock

case "$REVISION" in
  *[!0-9a-f]*|'') echo "git revision must be lowercase hexadecimal" >&2; exit 2 ;;
esac
test -f "$ARCHIVE"
test -f "$ROOT/.env"

exec 9>"$LOCK"
flock -n 9 || { echo "another Content Studio deployment is running" >&2; exit 1; }

install -d -m 0755 "$RELEASES"
install -d -o photoapp -g photoapp -m 0700 "$ROOT/data"
rm -rf -- "$RELEASE"
install -d -m 0755 "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"
printf '%s\n' "$REVISION" > "$RELEASE/REVISION"

test -f "$RELEASE/app/content_studio/cli.py"
test -f "$RELEASE/marketing/assets/approved/manifest.json"
test -f "$RELEASE/marketing/content/library.json"
test -f "$RELEASE/ops/ravuna-content-publisher.service"
test -f "$RELEASE/ops/ravuna-content-publisher.timer"
test -f "$RELEASE/requirements.txt"
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check --quiet -r "$RELEASE/requirements.txt"
"$VENV/bin/python" -m py_compile "$RELEASE"/app/content_studio/*.py

PREVIOUS=""
if [ -L "$ROOT/current" ] || [ -e "$ROOT/current" ]; then
  PREVIOUS=$(readlink -e "$ROOT/current" 2>/dev/null || true)
fi
ln -sfn "$RELEASE" "$ROOT/current.next"
mv -Tf "$ROOT/current.next" "$ROOT/current"
install -m 0644 "$RELEASE/ops/ravuna-content-publisher.service" /etc/systemd/system/ravuna-content-publisher.service
install -m 0644 "$RELEASE/ops/ravuna-content-publisher.timer" /etc/systemd/system/ravuna-content-publisher.timer
systemctl daemon-reload
systemctl enable ravuna-content-publisher.timer >/dev/null

if ! systemctl start ravuna-content-publisher.service; then
  if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ] && [ "$PREVIOUS" != "$ROOT/current" ]; then
    ln -sfn "$PREVIOUS" "$ROOT/current.next"
    mv -Tf "$ROOT/current.next" "$ROOT/current"
  else
    rm -f -- "$ROOT/current"
  fi
  echo "Content Studio first run failed; photo-bot was not touched" >&2
  exit 1
fi
systemctl start ravuna-content-publisher.timer
test "$(systemctl show ravuna-content-publisher.service -p Result --value)" = success
systemctl is-active --quiet ravuna-content-publisher.timer
printf 'content_studio_revision=%s\n' "$REVISION"
printf 'content_studio_timer=active\n'
