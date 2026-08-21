#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$TOOLS_DIR/platform_runtime_common.sh"

platform_load_env_file

cd "$PLATFORM_ROOT_DIR/apps/platform_web"

if [[ ! -f .next/standalone/server.js ]]; then
  echo "Missing .next/standalone/server.js. Run 'cd platform/apps/platform_web && ../../tools/platform_web_npm.sh run build' first." >&2
  exit 1
fi

export PLATFORM_API_INTERNAL_ORIGIN="${PLATFORM_API_INTERNAL_ORIGIN:-http://127.0.0.1:${PLATFORM_API_PORT:-8010}}"
export HOSTNAME="${PLATFORM_WEB_BIND_HOST:-127.0.0.1}"
export PORT="${PLATFORM_WEB_PORT:-${PORT:-3000}}"

exec "$PLATFORM_NODE_BIN" \
  --require "$PLATFORM_ROOT_DIR/apps/platform_web/server-shutdown-guard.cjs" \
  .next/standalone/server.js
