#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$TOOLS_DIR/platform_runtime_common.sh"

platform_require_isolated_service_env worker
platform_load_env_file
platform_require_python

cd "$PLATFORM_ROOT_DIR"
export PYTHONPATH="$PLATFORM_ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"

exec "$PLATFORM_PYTHON_BIN" -m celery \
  -A apps.platform_worker.worker:celery_app worker \
  --beat \
  --queues deadlock-platform-high,deadlock-platform-default,deadlock-platform-low \
  --concurrency "${PLATFORM_WORKER_CONCURRENCY:-2}" \
  --schedule "$PLATFORM_SHARED_DIR/worker-state/celerybeat-schedule" \
  --loglevel INFO
