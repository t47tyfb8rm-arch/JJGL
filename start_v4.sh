#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/fund-manager-v4"
BACKEND_DIR="$APP_DIR/web-app/backend"
PYTHON="/opt/fund-manager/venv/bin/python"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8001}"
PID_FILE="$APP_DIR/backend.pid"
LOG_FILE="$APP_DIR/backend.log"

mkdir -p "$BACKEND_DIR"

if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    if curl -fsS "http://127.0.0.1:$PORT/v4/" >/dev/null 2>&1; then
      echo "[INFO] V4 backend already running, PID=$OLD_PID, port=$PORT"
      exit 0
    fi
    echo "[WARN] V4 PID=$OLD_PID exists but port=$PORT is not responding; restarting"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

cd "$BACKEND_DIR"
export FUND_MANAGER_DB_PATH="$BACKEND_DIR/fund_manager_v4.db"
export PORT="$PORT"

nohup "$PYTHON" -m uvicorn main:app --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

sleep 2
if curl -fsS "http://127.0.0.1:$PORT/v4/" >/dev/null; then
  echo "[OK] V4 backend started, PID=$(cat "$PID_FILE"), port=$PORT"
else
  echo "[ERR] V4 backend failed, see $LOG_FILE"
  tail -40 "$LOG_FILE" || true
  exit 1
fi
