#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/photo-bot.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "photo-bot is not running (PID file is absent)"
  exit 0
fi

pid="$(cat "$PID_FILE")"
if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
  echo "Invalid PID file; refusing to signal any process" >&2
  exit 1
fi

if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Removed stale photo-bot PID file"
  exit 0
fi

command_line="$(ps -p "$pid" -o args= 2>/dev/null || true)"
if [[ "$command_line" != *"-m app.main run"* ]]; then
  echo "PID $pid does not belong to photo-bot; refusing to stop it" >&2
  exit 1
fi

kill "$pid"
for _ in {1..20}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "photo-bot stopped"
    exit 0
  fi
  sleep 1
done

echo "photo-bot did not stop after SIGTERM; PID file retained" >&2
exit 1
