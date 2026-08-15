#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${DIALECTICORE_DEV_APP_DIR:-/srv/DialectiCore}"
ENV_FILE="${DIALECTICORE_DEV_ENV_FILE:-$APP_DIR/.env}"
UNIT_PREFIX="${DIALECTICORE_DEV_WORKER_UNIT_PREFIX:-dialecticore-worker-dev}"
DEFAULT_ROLES="workflow-worker render-worker"
STAGE_ROLES="discussion-worker research-worker localization-worker voicebox-adapter comfyui-adapter timeline-worker render-worker qc-worker publishing-worker"
ALL_ROLES="$DEFAULT_ROLES $STAGE_ROLES temporal-worker"
POLL_INTERVAL="${DIALECTICORE_DEV_WORKER_POLL_INTERVAL_SECONDS:-5}"

cd "$APP_DIR"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

if [[ -n "${DIALECTICORE_DEV_WORKER_ROLES:-}" ]]; then
  ROLES="$DIALECTICORE_DEV_WORKER_ROLES"
elif [[ "${DIALECTICORE_TEMPORAL_BACKEND_MODE:-local}" == "external" && "${DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED:-false}" == "true" ]]; then
  ROLES="$DEFAULT_ROLES temporal-worker"
else
  ROLES="$DEFAULT_ROLES"
fi

if [[ "$ROLES" == "all" ]]; then
  ROLES="$ALL_ROLES"
fi

systemctl --user daemon-reload

env_args=(
  "--setenv=PYTHONPATH=$APP_DIR/backend"
  "--setenv=DIALECTICORE_WORKER_POLL_INTERVAL_SECONDS=$POLL_INTERVAL"
)
if [[ -n "${B1_API_KEY:-}" ]]; then
  env_args+=("--setenv=B1_API_KEY=$B1_API_KEY")
fi
if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  env_args+=("--setenv=OPENROUTER_API_KEY=$OPENROUTER_API_KEY")
fi

for role in $ALL_ROLES; do
  unit="${UNIT_PREFIX}-${role}"
  persistent_unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit.service"
  if [[ -f "$persistent_unit" ]]; then
    if [[ " $ROLES " == *" $role "* ]]; then
      continue
    fi
    systemctl --user stop "$unit.service" >/dev/null 2>&1 || true
    continue
  fi
  systemctl --user kill -s SIGKILL "$unit.service" >/dev/null 2>&1 || true
  systemctl --user reset-failed "$unit.service" >/dev/null 2>&1 || true
  systemctl --user stop "$unit.service" >/dev/null 2>&1 || true
  systemctl --user reset-failed "$unit.service" >/dev/null 2>&1 || true
done

for role in $ROLES; do
  unit="${UNIT_PREFIX}-${role}"
  persistent_unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit.service"
  if [[ -f "$persistent_unit" ]]; then
    systemctl --user start "$unit.service"
  else
    systemd-run --user \
      --unit="$unit" \
      --working-directory="$APP_DIR" \
      "${env_args[@]}" \
      "--setenv=DIALECTICORE_WORKER_ROLE=$role" \
      "$APP_DIR/.venv/bin/python" -m app.workflows.worker_placeholder
  fi
done

echo "Started DialectiCore dev worker units:"
for role in $ROLES; do
  echo "  ${UNIT_PREFIX}-${role}.service"
done
