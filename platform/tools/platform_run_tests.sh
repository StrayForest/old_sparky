#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PLATFORM_SHARED_DIR="$ROOT_DIR"
export PLATFORM_ENV_FILE="${PLATFORM_ENV_FILE:-$ROOT_DIR/.env.platform}"
if [[ "${PLATFORM_TEST_AGGREGATE_ONLY:-0}" == "1" ]]; then
  export PLATFORM_PYTHON_BIN="${PLATFORM_PYTHON_BIN:-/usr/bin/python3}"
else
  export PLATFORM_PYTHON_BIN="$ROOT_DIR/.venv_platform/bin/python"
fi

# shellcheck source=./platform/tools/platform_runtime_common.sh
source "$ROOT_DIR/tools/platform_runtime_common.sh"
platform_require_python
platform_load_env_file

# Keep this early check in the shell entry point, but delegate all policy to
# the same pure validator used by the Python runner.  The runner repeats the
# validation after argument parsing, so bypassing this wrapper cannot bypass
# the safety boundary.  Aggregate-only CI uses the system interpreter; the
# validator intentionally has no application/client-library dependency.
"$PLATFORM_PYTHON_BIN" -c '
from tools.platform_test_runner import validate_test_resource_configuration
try:
    validate_test_resource_configuration()
except Exception as exc:
    raise SystemExit(f"LOCAL GATE BLOCKED: {exc}") from exc
'

exec "$PLATFORM_PYTHON_BIN" tools/platform_test_runner.py "$@"
