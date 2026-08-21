#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$TOOLS_DIR/platform_node.sh" --npm "$@"
