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
PREPARE_ONLY=${RAVUNA_CONTENT_DEPLOY_PREPARE_ONLY:-0}
RUNTIME_USER=photoapp
case "$PREPARE_ONLY" in
  0) ROOT=/opt/ravuna-content ;;
  1)
    ROOT=${RAVUNA_CONTENT_DEPLOY_ROOT:?prepare-only root is required}
    RUNTIME_USER=${RAVUNA_CONTENT_DEPLOY_RUNTIME_USER:-nobody}
    case "$(readlink -m "$ROOT")" in
      /tmp/*|/var/tmp/*) ;;
      *) echo "prepare-only root must be under /tmp or /var/tmp" >&2; exit 2 ;;
    esac
    ;;
  *) echo "RAVUNA_CONTENT_DEPLOY_PREPARE_ONLY must be 0 or 1" >&2; exit 2 ;;
esac
RELEASES=$ROOT/releases
RELEASE=$RELEASES/$REVISION
VENV=$ROOT/venv
if [ "$PREPARE_ONLY" = 1 ]; then
  LOCK=$ROOT/deploy.lock
else
  LOCK=/run/lock/ravuna-content-deploy.lock
fi

case "$REVISION" in
  *[!0-9a-f]*|'') echo "git revision must be lowercase hexadecimal" >&2; exit 2 ;;
esac
test -f "$ARCHIVE"
test -f "$ROOT/.env"

exec 9>"$LOCK"
flock -n 9 || { echo "another Content Studio deployment is running" >&2; exit 1; }

install -d -m 0755 "$RELEASES"
if [ ! -d "$ROOT/data" ]; then
  install -d -o "$RUNTIME_USER" -g "$(id -gn "$RUNTIME_USER")" -m 0700 "$ROOT/data"
fi
rm -rf -- "$RELEASE"
install -d -m 0755 "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"
printf '%s\n' "$REVISION" > "$RELEASE/REVISION"

# Tar archives may restore a restrictive mode on the extraction root. Keep the
# immutable release root-owned while preserving executable bits and making all
# directories traversable and files readable by the runtime user.
chown -R root:root -- "$RELEASE"
chmod -R u=rwX,go=rX -- "$RELEASE"

test -f "$RELEASE/app/content_studio/cli.py"
test -f "$RELEASE/marketing/assets/approved/manifest.json"
test -f "$RELEASE/marketing/content/library.json"
test -f "$RELEASE/ops/ravuna-content-publisher.service"
test -f "$RELEASE/ops/ravuna-content-publisher.timer"
test -f "$RELEASE/requirements.txt"

runuser -u "$RUNTIME_USER" -- test -x "$RELEASE"
for required in \
  app/content_studio/cli.py \
  marketing/assets/approved/manifest.json \
  marketing/content/library.json \
  ops/ravuna-content-publisher.service \
  ops/ravuna-content-publisher.timer \
  requirements.txt
do
  runuser -u "$RUNTIME_USER" -- test -r "$RELEASE/$required"
done

if [ "$PREPARE_ONLY" = 1 ]; then
  printf 'content_studio_preflight=PASS\n'
  exit 0
fi

command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check --quiet -r "$RELEASE/requirements.txt"
"$VENV/bin/python" -m py_compile "$RELEASE"/app/content_studio/*.py
runuser -u "$RUNTIME_USER" -- /bin/sh -c '
  cd "$1"
  PYTHONDONTWRITEBYTECODE=1 "$2" -c "import app.content_studio.cli"
' sh "$RELEASE" "$VENV/bin/python"

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
