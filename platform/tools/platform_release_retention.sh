#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$TOOLS_DIR/platform_runtime_common.sh"

platform_require_python

exec "$PLATFORM_PYTHON_BIN" "$TOOLS_DIR/platform_release_retention.py" "$@"
