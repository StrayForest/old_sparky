#!/usr/bin/env bash
set +x
set -euo pipefail

TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_manual_live_auth_qa.sh"
SYSTEM_PYTHON="/usr/bin/python3.12"
QA_PYTHON="$PLATFORM_ROOT/.venv_platform/bin/python"

if [[ "$EUID" -ne 0 ]]; then
  echo "Manual live auth QA must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Manual live auth QA must run from the fixed root-controlled checkout." >&2
  exit 1
fi
if [[ "${PLATFORM_APP_DIR:-}" != "/opt/oldsparky/platform" ]]; then
  echo "Manual live auth QA requires the fixed production runtime contour." >&2
  exit 1
fi
if [[ -n "${PLATFORM_LIVE_CSP_ALLOW_LOOPBACK:-}" ]]; then
  echo "Manual production auth QA does not permit loopback mode." >&2
  exit 1
fi
if [[ ! -x "$QA_PYTHON" ]]; then
  echo "Root-controlled checkout Python runtime is unavailable." >&2
  exit 1
fi
: "${PLATFORM_LIVE_CSP_QA_BUNDLE:?PLATFORM_LIVE_CSP_QA_BUNDLE must point to the root-only CSP QA bundle}"
if [[ "$PLATFORM_LIVE_CSP_QA_BUNDLE" != /* ]]; then
  echo "PLATFORM_LIVE_CSP_QA_BUNDLE must be an absolute path." >&2
  exit 1
fi

usage() {
  echo "Usage: $0 prepare|show-email|show-display-name|show-password registration|reset|code email-verification|password-reset|attest-and-cleanup|abort-and-cleanup" >&2
}

if (( $# < 1 )); then
  usage
  exit 2
fi
COMMAND="$1"
shift

GUARD=("$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_live_qa_guard.py")
if [[ -z "${PLATFORM_LIVE_QA_LOCK_FD:-}" ]]; then
  exec "${GUARD[@]}" locked-exec \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    -- "$SCRIPT_PATH" "$COMMAND" "$@"
fi
"${GUARD[@]}" assert-lock \
  --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
  --fd "$PLATFORM_LIVE_QA_LOCK_FD"
"${GUARD[@]}" verify-provenance \
  --platform-root "$PLATFORM_ROOT" \
  --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" >/dev/null
"$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" validate-runtime
"${GUARD[@]}" preflight \
  --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
  --mode manual-prepare

run_database_command() {
  exec "$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" exec \
    --pythonpath "$PLATFORM_ROOT" \
    -- "$QA_PYTHON" "$TOOLS_DIR/platform_manual_live_auth_qa.py" \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" "$@"
}

run_secret_command() {
  exec /usr/bin/env -i \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONPATH="$PLATFORM_ROOT" \
    PLATFORM_LIVE_CSP_QA_BUNDLE="$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    "$QA_PYTHON" "$TOOLS_DIR/platform_manual_live_auth_qa.py" \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" "$@"
}

case "$COMMAND" in
  prepare|attest-and-cleanup|abort-and-cleanup)
    (( $# == 0 )) || { usage; exit 2; }
    cd "$PLATFORM_ROOT"
    run_database_command "$COMMAND"
    ;;
  show-email|show-display-name)
    (( $# == 0 )) || { usage; exit 2; }
    cd "$PLATFORM_ROOT"
    run_secret_command "$COMMAND"
    ;;
  show-password)
    if (( $# != 1 )) || [[ "$1" != "registration" && "$1" != "reset" ]]; then
      usage
      exit 2
    fi
    cd "$PLATFORM_ROOT"
    run_secret_command show-password "$1"
    ;;
  code)
    if (( $# != 1 )) || [[ "$1" != "email-verification" && "$1" != "password-reset" ]]; then
      usage
      exit 2
    fi
    cd "$PLATFORM_ROOT"
    run_secret_command code "$1"
    ;;
  *)
    usage
    exit 2
    ;;
esac
