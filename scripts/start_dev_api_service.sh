#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${DIALECTICORE_DEV_APP_DIR:-/srv/DialectiCore}"
ENV_FILE="${DIALECTICORE_DEV_ENV_FILE:-$APP_DIR/.env}"
UNIT_NAME="${DIALECTICORE_DEV_API_UNIT:-dialecticore-api-dev}"
HOST="${DIALECTICORE_DEV_API_HOST:-127.0.0.1}"
PORT="${DIALECTICORE_DEV_API_PORT:-8000}"

cd "$APP_DIR"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

env_args=()
if [[ -n "${B1_API_KEY:-}" ]]; then
  env_args+=("--setenv=B1_API_KEY=$B1_API_KEY")
fi
if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  env_args+=("--setenv=OPENROUTER_API_KEY=$OPENROUTER_API_KEY")
fi

persistent_unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT_NAME.service"
if [[ -f "$persistent_unit" ]]; then
  systemctl --user daemon-reload
  systemctl --user start "$UNIT_NAME.service"
  echo "Started persistent unit: $UNIT_NAME.service"
  exit 0
fi

systemctl --user kill -s SIGKILL "$UNIT_NAME.service" >/dev/null 2>&1 || true
systemctl --user reset-failed "$UNIT_NAME.service" >/dev/null 2>&1 || true
systemctl --user stop "$UNIT_NAME.service" >/dev/null 2>&1 || true
systemctl --user reset-failed "$UNIT_NAME.service" >/dev/null 2>&1 || true

systemd-run --user \
  --unit="$UNIT_NAME" \
  --working-directory="$APP_DIR" \
  "${env_args[@]}" \
  "$APP_DIR/.venv/bin/uvicorn" app.main:app \
  --app-dir backend \
  --host "$HOST" \
  --port "$PORT"
