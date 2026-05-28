#!/usr/bin/env bash
set -euo pipefail

APP_PORT="${APP_PORT:-8000}"
APP_HOST="${APP_HOST:-0.0.0.0}"
VENV_DIR="${VENV_DIR:-.venv}"
VENV_PY="${VENV_PY:-$VENV_DIR/Scripts/python}"
BOOTSTRAP="${BOOTSTRAP:-1}"
REQ_FILE="${REQ_FILE:-requirements.txt}"
APP_CMD="${APP_CMD:-\"$VENV_PY\" -m app.main}"
TUNNEL_CMD="${TUNNEL_CMD:-devtunnel host -p ${APP_PORT} --allow-anonymous}"

APP_LOG="${APP_LOG:-devtunnel-app.log}"
TUNNEL_LOG="${TUNNEL_LOG:-devtunnel-host.log}"

cleanup() {
  local exit_code=$?
  if [[ -n "${APP_PID:-}" ]] && kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
  fi
  if [[ -n "${TUNNEL_PID:-}" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
    kill "$TUNNEL_PID" 2>/dev/null || true
    wait "$TUNNEL_PID" 2>/dev/null || true
  fi
  exit "$exit_code"
}

trap cleanup EXIT INT TERM

mkdir -p "$(dirname "$APP_LOG")" "$(dirname "$TUNNEL_LOG")"

if [[ "$BOOTSTRAP" == "1" ]]; then
  if [[ ! -x "$VENV_PY" ]]; then
    echo "Creating venv at $VENV_DIR"
    python -m venv "$VENV_DIR"
  fi
  if [[ -f "$REQ_FILE" ]]; then
    echo "Installing requirements from $REQ_FILE"
    "$VENV_PY" -m pip install -r "$REQ_FILE"
  fi
fi

echo "Starting app: $APP_CMD"
nohup bash -lc "$APP_CMD" >"$APP_LOG" 2>&1 &
APP_PID=$!

echo "Starting devtunnel: $TUNNEL_CMD"
nohup bash -lc "$TUNNEL_CMD" >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

echo "App PID: $APP_PID (logs: $APP_LOG)"
echo "Tunnel PID: $TUNNEL_PID (logs: $TUNNEL_LOG)"
echo "Press Ctrl+C to stop both."

while true; do
  sleep 1
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "App process exited."
    exit 1
  fi
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "Tunnel process exited."
    exit 1
  fi
done
