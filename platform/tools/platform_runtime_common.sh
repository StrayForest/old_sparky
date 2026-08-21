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

_platform_shared_node_current_bin="$PLATFORM_SHARED_DIR/node-current/bin/node"
_platform_shared_node_26_bin="$PLATFORM_SHARED_DIR/node-v26.3.1/bin/node"
if [[ -z "${PLATFORM_NODE_BIN:-}" ]]; then
  if [[ -x "$_platform_shared_node_current_bin" ]]; then
    PLATFORM_NODE_BIN="$_platform_shared_node_current_bin"
  elif [[ -x "$_platform_shared_node_26_bin" ]]; then
    PLATFORM_NODE_BIN="$_platform_shared_node_26_bin"
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

platform_load_env_file() {
  if [[ -f "$PLATFORM_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$PLATFORM_ENV_FILE"
    set +a
  fi
}

platform_require_python() {
  if [[ ! -x "$PLATFORM_PYTHON_BIN" ]]; then
    echo "Missing Python runtime at $PLATFORM_PYTHON_BIN." >&2
    echo "Set PLATFORM_PYTHON_BIN or create the shared venv before starting services." >&2
    exit 1
  fi
}
