#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$TOOLS_DIR/platform_runtime_common.sh"

platform_require_python

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
  echo "Usage: $0 BACKUP.dump [platform_backup_restore_drill.py options]"
  echo "Restores BACKUP.dump into an isolated temporary database, verifies it, and removes it."
  exit 0
fi

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 BACKUP.dump [platform_backup_restore_drill.py options]" >&2
  exit 2
fi

DUMP_PATH="$1"
shift
exec "$PLATFORM_PYTHON_BIN" "$TOOLS_DIR/platform_backup_restore_drill.py" \
  --verify-dump "$DUMP_PATH" "$@"
