#!/usr/bin/env bash

PLATFORM_ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_platform_root_parent="$(cd "$PLATFORM_ROOT_DIR/.." && pwd)"
if [[ "$(basename "$_platform_root_parent")" == "releases" && -d "$_platform_root_parent/../shared" ]]; then
  _platform_default_app_dir="$(cd "$_platform_root_parent/.." && pwd)"
else
  _platform_default_app_dir="$_platform_root_parent"
fi

PLATFORM_APP_DIR="${PLATFORM_APP_DIR:-$_platform_default_app_dir}"

if [[ -d "$PLATFORM_APP_DIR/shared" && -d "$PLATFORM_APP_DIR/releases" ]]; then
  _platform_default_shared_dir="$PLATFORM_APP_DIR/shared"
else
  _platform_default_shared_dir="$PLATFORM_ROOT_DIR"
fi

PLATFORM_SHARED_DIR="${PLATFORM_SHARED_DIR:-$_platform_default_shared_dir}"

_platform_local_env_file="$PLATFORM_ROOT_DIR/.env.platform"
_platform_shared_env_file="$PLATFORM_SHARED_DIR/.env.platform"
if [[ -z "${PLATFORM_ENV_FILE:-}" ]]; then
  if [[ -f "$_platform_shared_env_file" && "$_platform_shared_env_file" != "$_platform_local_env_file" ]]; then
    PLATFORM_ENV_FILE="$_platform_shared_env_file"
  else
    PLATFORM_ENV_FILE="$_platform_local_env_file"
  fi
fi

_platform_local_python_bin="$PLATFORM_ROOT_DIR/.venv_platform/bin/python"
_platform_shared_python_bin="$PLATFORM_SHARED_DIR/venv/bin/python"
if [[ -z "${PLATFORM_PYTHON_BIN:-}" ]]; then
  if [[ -x "$_platform_shared_python_bin" && "$_platform_shared_python_bin" != "$_platform_local_python_bin" ]]; then
    PLATFORM_PYTHON_BIN="$_platform_shared_python_bin"
  else
    PLATFORM_PYTHON_BIN="$_platform_local_python_bin"
  fi
fi

_platform_shared_node_26_bin="$PLATFORM_SHARED_DIR/node-v26.3.1/bin/node"
_platform_shared_node_current_bin="$PLATFORM_SHARED_DIR/node-current/bin/node"
if [[ -z "${PLATFORM_NODE_BIN:-}" ]]; then
  if [[ -x "$_platform_shared_node_26_bin" ]]; then
    PLATFORM_NODE_BIN="$_platform_shared_node_26_bin"
  elif [[ -x "$_platform_shared_node_current_bin" ]]; then
    PLATFORM_NODE_BIN="$_platform_shared_node_current_bin"
  else
    PLATFORM_NODE_BIN="/usr/bin/node"
  fi
fi

export PLATFORM_ROOT_DIR
export PLATFORM_APP_DIR
export PLATFORM_SHARED_DIR
export PLATFORM_ENV_FILE
export PLATFORM_PYTHON_BIN
export PLATFORM_NODE_BIN

platform_require_isolated_service_env() {
  local expected_service="$1"
  local declared_service="${PLATFORM_RUNTIME_SERVICE:-}"
  if [[ -z "$declared_service" ]]; then
    return 0
  fi
  if [[ "$declared_service" != "$expected_service" ]]; then
    echo "Runtime service mismatch: expected $expected_service, got $declared_service." >&2
    exit 1
  fi
  local expected_env="$PLATFORM_SHARED_DIR/env/${expected_service}.env"
  if [[ "$PLATFORM_ENV_FILE" != "$expected_env" ]]; then
    echo "$expected_service must use isolated env $expected_env, not $PLATFORM_ENV_FILE." >&2
    exit 1
  fi
  if [[ ! -f "$PLATFORM_ENV_FILE" || -L "$PLATFORM_ENV_FILE" ]]; then
    echo "Isolated runtime env is missing or unsafe: $PLATFORM_ENV_FILE" >&2
    exit 1
  fi
}

platform_load_env_file() {
  if [[ ! -f "$PLATFORM_ENV_FILE" ]]; then
    return 0
  fi
  local safe_env_tool="$PLATFORM_ROOT_DIR/tools/platform_safe_env_exec.py"
  if [[ ! -f "$safe_env_tool" || -L "$safe_env_tool" ]]; then
    echo "Safe environment parser is missing or unsafe: $safe_env_tool" >&2
    exit 1
  fi
  local encoded_assignments
  if ! encoded_assignments="$(
    /usr/bin/python3 -I "$safe_env_tool" export-b64 --path "$PLATFORM_ENV_FILE"
  )"; then
    echo "Refusing unsafe platform environment: $PLATFORM_ENV_FILE" >&2
    exit 1
  fi
  local key encoded value
  while IFS=$'\t' read -r key encoded; do
    [[ -n "$key" ]] || continue
    if ! value="$(printf '%s' "$encoded" | /usr/bin/base64 --decode)"; then
      echo "Failed to decode platform environment value for: $key" >&2
      exit 1
    fi
    printf -v "$key" '%s' "$value"
    export "$key"
  done <<<"$encoded_assignments"
}

platform_require_python() {
  if [[ ! -x "$PLATFORM_PYTHON_BIN" ]]; then
    echo "Missing Python runtime at $PLATFORM_PYTHON_BIN." >&2
    echo "Set PLATFORM_PYTHON_BIN or create the shared venv before starting services." >&2
    exit 1
  fi
}
