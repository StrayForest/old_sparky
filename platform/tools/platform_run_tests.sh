#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
export PLATFORM_SHARED_DIR="$ROOT_DIR"
export PLATFORM_ENV_FILE="${PLATFORM_ENV_FILE:-$ROOT_DIR/.env.platform}"
_platform_ci_venv_dir="${PLATFORM_CI_VENV_DIR:-}"
_platform_ci_python_bin=""
if [[ -n "$_platform_ci_venv_dir" ]]; then
  if [[ "$_platform_ci_venv_dir" != /* ]]; then
    _platform_ci_venv_dir="$ROOT_DIR/$_platform_ci_venv_dir"
  fi
  if ! _platform_ci_venv_dir="$(cd -- "$_platform_ci_venv_dir" 2>/dev/null && pwd -P)"; then
    echo "LOCAL GATE BLOCKED: PLATFORM_CI_VENV_DIR is unavailable." >&2
    exit 127
  fi
  case "$_platform_ci_venv_dir/" in
    "$ROOT_DIR/"*) ;;
    *)
      echo "LOCAL GATE BLOCKED: PLATFORM_CI_VENV_DIR must be under the platform root." >&2
      exit 2
      ;;
  esac
  _platform_ci_python_bin="$_platform_ci_venv_dir/bin/python"
  if [[ ! -x "$_platform_ci_python_bin" ]]; then
    echo "LOCAL GATE BLOCKED: pinned CI Python runtime is unavailable." >&2
    exit 127
  fi
fi
if [[ "${PLATFORM_TEST_AGGREGATE_ONLY:-0}" == "1" ]]; then
  export PLATFORM_PYTHON_BIN="${PLATFORM_PYTHON_BIN:-/usr/bin/python3}"
elif [[ -n "$_platform_ci_python_bin" ]]; then
  export PLATFORM_PYTHON_BIN="$_platform_ci_python_bin"
else
  export PLATFORM_PYTHON_BIN="$ROOT_DIR/.venv_platform/bin/python"
fi

# shellcheck source=./platform/tools/platform_runtime_common.sh
source "$ROOT_DIR/tools/platform_runtime_common.sh"
platform_require_python
platform_load_env_file
if [[ -n "$_platform_ci_python_bin" && "${PLATFORM_TEST_AGGREGATE_ONLY:-0}" != "1" ]]; then
  export PLATFORM_CI_VENV_DIR="$_platform_ci_venv_dir"
  export PLATFORM_PYTHON_BIN="$_platform_ci_python_bin"
fi

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
