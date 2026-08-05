#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/fund-manager-v4"
PID_FILE="$APP_DIR/backend.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "[INFO] V4 backend is not running"
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "[INFO] stopping V4 backend PID=$PID"
  kill "$PID" || true
  for _ in 1 2 3 4 5; do
    if ! kill -0 "$PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done
fi

rm -f "$PID_FILE"
echo "[OK] V4 backend stopped"
