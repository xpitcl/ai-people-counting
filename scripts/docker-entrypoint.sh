#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/app}"
CONFIG_PATH="/data/config.json"
TEMPLATE_PATH="${APP_DIR}/config_template.json"

mkdir -p "$(dirname "${CONFIG_PATH}")" "${HOME}/.cache/ai-people-counting"

python3 "${APP_DIR}/scripts/render_config_from_env.py" "${TEMPLATE_PATH}" "${CONFIG_PATH}"

if [[ "${SETUP_PEOPLENET:-1}" == "1" ]]; then
  "${APP_DIR}/scripts/setup_peoplenet.sh"
fi

if [[ "${1:-}" == "python3" || "${1:-}" == "python" ]]; then
  exec "$@"
fi

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

cd "${APP_DIR}"

args=(python3 people_counter_jetson.py --config "${CONFIG_PATH}")
args+=(--backend "${BACKEND:-deepstream}")
if [[ "${NO_DISPLAY:-1}" == "1" ]]; then
  args+=(--no-display)
fi
exec "${args[@]}"
