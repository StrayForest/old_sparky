#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$TOOLS_DIR/platform_runtime_common.sh"

platform_require_isolated_service_env api
platform_load_env_file
platform_require_python

cd "$PLATFORM_ROOT_DIR"
export PYTHONPATH="$PLATFORM_ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export FORWARDED_ALLOW_IPS="${PLATFORM_API_FORWARDED_ALLOW_IPS:-127.0.0.1}"

if "$PLATFORM_PYTHON_BIN" -c "import gunicorn" >/dev/null 2>&1; then
  exec "$PLATFORM_PYTHON_BIN" -m gunicorn apps.platform_api.app.main:app \
    --bind "${PLATFORM_API_HOST:-127.0.0.1}:${PLATFORM_API_PORT:-8010}" \
    --workers "${PLATFORM_API_WORKERS:-2}" \
    --worker-class uvicorn.workers.UvicornWorker \
    --timeout "${PLATFORM_API_WORKER_TIMEOUT:-120}" \
    --graceful-timeout "${PLATFORM_API_GRACEFUL_TIMEOUT:-30}" \
    --keep-alive "${PLATFORM_API_KEEPALIVE:-5}" \
    --access-logfile "-" \
    --error-logfile "-" \
    --log-level "${PLATFORM_GUNICORN_LOG_LEVEL:-info}"
fi

exec "$PLATFORM_PYTHON_BIN" -m uvicorn apps.platform_api.app.main:app \
  --host "${PLATFORM_API_HOST:-127.0.0.1}" \
  --port "${PLATFORM_API_PORT:-8010}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS}"
