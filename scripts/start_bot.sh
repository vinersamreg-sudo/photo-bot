#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/venv/bin/python"
PID_FILE="$ROOT_DIR/data/photo-bot.pid"
LOG_FILE="$ROOT_DIR/logs/app.log"
NOHUP_LOG="$ROOT_DIR/logs/nohup.log"

if [[ ! -x "$PYTHON" ]]; then
  echo "Virtualenv Python not found: $PYTHON" >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "photo-bot is already running (PID $pid)"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

cd "$ROOT_DIR"
nohup "$PYTHON" -m app.main run >>"$NOHUP_LOG" 2>&1 &
pid=$!
echo "$pid" >"$PID_FILE"

sleep 1
if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "photo-bot failed to start; inspect $LOG_FILE" >&2
  exit 1
fi

echo "photo-bot started (PID $pid)"
