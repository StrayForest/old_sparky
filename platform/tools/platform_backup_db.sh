#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$TOOLS_DIR/platform_runtime_common.sh"

platform_require_python

# The shell wrapper is a compatibility entrypoint; the old
# platform_backup_restore_drill.py remains a library primitive and mutation is owned by the
# lock/evidence supervisor.  Read-only freshness diagnostics should call the
# restore helper explicitly with --check-latest.
exec "$PLATFORM_PYTHON_BIN" "$TOOLS_DIR/platform_backup_supervisor.py" backup "$@"
