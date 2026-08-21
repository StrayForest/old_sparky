#!/usr/bin/env bash
set +x
set -euo pipefail

TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_provision_live_csp_qa.sh"
SYSTEM_PYTHON="/usr/bin/python3.12"
QA_PYTHON="$PLATFORM_ROOT/.venv_platform/bin/python"

if [[ "$EUID" -ne 0 ]]; then
  echo "Live CSP QA provisioning must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Live CSP QA provisioning must run from the fixed root-controlled checkout." >&2
  exit 1
fi
if [[ "${PLATFORM_APP_DIR:-}" != "/opt/oldsparky/platform" ]]; then
  echo "Live CSP QA provisioning requires the fixed production runtime contour." >&2
  exit 1
fi
if [[ ! -x "$QA_PYTHON" ]]; then
  echo "Root-controlled checkout Python runtime is unavailable." >&2
  exit 1
fi

BUNDLE_PATH=""
HELPER_PATH=""
previous=""
for argument in "$@"; do
  if [[ "$previous" == "bundle" ]]; then
    [[ -z "$BUNDLE_PATH" ]] || { echo "--bundle-path must be specified once." >&2; exit 2; }
    BUNDLE_PATH="$argument"
    previous=""
  elif [[ "$previous" == "helper" ]]; then
    [[ -z "$HELPER_PATH" ]] || { echo "--mailbox-helper must be specified once." >&2; exit 2; }
    HELPER_PATH="$argument"
    previous=""
  elif [[ "$argument" == "--bundle-path" ]]; then
    previous="bundle"
  elif [[ "$argument" == "--mailbox-helper" ]]; then
    previous="helper"
  fi
done
if [[ -n "$previous" || -z "$BUNDLE_PATH" || -z "$HELPER_PATH" \
  || "$BUNDLE_PATH" != /* || "$HELPER_PATH" != /* ]]; then
  echo "Provisioning requires one absolute --bundle-path and --mailbox-helper." >&2
  exit 2
fi

GUARD=("$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_live_qa_guard.py")
if [[ -z "${PLATFORM_LIVE_QA_LOCK_FD:-}" ]]; then
  exec "${GUARD[@]}" locked-exec \
    --bundle-path "$BUNDLE_PATH" \
    -- "$SCRIPT_PATH" "$@"
fi
"${GUARD[@]}" assert-lock \
  --bundle-path "$BUNDLE_PATH" \
  --fd "$PLATFORM_LIVE_QA_LOCK_FD"
"${GUARD[@]}" verify-provenance \
  --platform-root "$PLATFORM_ROOT" \
  --helper-path "$HELPER_PATH" >/dev/null
"${GUARD[@]}" preflight \
  --bundle-path "$BUNDLE_PATH" \
  --mode provision

cd "$PLATFORM_ROOT"
exec "$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" exec \
  --pythonpath "$PLATFORM_ROOT" \
  -- "$QA_PYTHON" "$TOOLS_DIR/platform_provision_live_csp_qa.py" \
  provision "$@"
