#!/usr/bin/env bash
set +x
set -euo pipefail

TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_live_user_qa.sh"
SYSTEM_PYTHON="/usr/bin/python3.12"
QA_PYTHON="$PLATFORM_ROOT/.venv_platform/bin/python"

usage() {
  echo "Usage: $0 | $0 recover /absolute/path/to/live-user-qa.XXXXXX | $0 recover-setup /absolute/path/to/.live-user-qa.setup-<id>" >&2
}

if [[ "$EUID" -ne 0 ]]; then
  echo "Live-user QA supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Live-user QA must run from the fixed root-controlled checkout." >&2
  exit 1
fi
if [[ "${PLATFORM_APP_DIR:-}" != "/opt/oldsparky/platform" ]]; then
  echo "Live-user QA requires the fixed production runtime contour." >&2
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
if [[ -n "${PLATFORM_LIVE_CSP_ALLOW_LOOPBACK:-}" ]]; then
  echo "The production live-user QA wrapper does not permit loopback mode." >&2
  exit 1
fi
if (( $# != 0 )) && ! { (( $# == 2 )) \
  && [[ "$1" == "recover" || "$1" == "recover-setup" ]] \
  && [[ "$2" == /* ]]; }; then
  usage
  exit 2
fi

GUARD=("$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_live_qa_guard.py")
if [[ -z "${PLATFORM_LIVE_QA_LOCK_FD:-}" ]]; then
  LOCK_COMMAND="locked-exec"
  if (( $# == 2 )) && [[ "$1" == "recover" || "$1" == "recover-setup" ]]; then
    LOCK_COMMAND="recovery-locked-exec"
  fi
  exec "${GUARD[@]}" "$LOCK_COMMAND" \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    -- "$SCRIPT_PATH" "$@"
fi
"${GUARD[@]}" assert-lock \
  --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
  --fd "$PLATFORM_LIVE_QA_LOCK_FD"
SOURCE_COMMIT="$(
  "${GUARD[@]}" verify-provenance \
    --platform-root "$PLATFORM_ROOT" \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE"
)"

EXPECTED_LIVE_ORIGIN="$(
  "$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" \
    print-public-value PLATFORM_WEB_ORIGIN
)"
EXPECTED_ENVIRONMENT="$(
  "$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" \
    print-public-value PLATFORM_ENVIRONMENT
)"
if [[ "$EXPECTED_ENVIRONMENT" != "production" \
  || "$EXPECTED_LIVE_ORIGIN" != "https://old-sparky.com" ]]; then
  echo "Live-user QA requires the canonical production origin https://old-sparky.com." >&2
  exit 1
fi
if [[ -n "${PLAYWRIGHT_LIVE_BASE_URL:-}" \
  && "$PLAYWRIGHT_LIVE_BASE_URL" != "$EXPECTED_LIVE_ORIGIN" ]]; then
  echo "PLAYWRIGHT_LIVE_BASE_URL must equal the canonical production origin." >&2
  exit 1
fi

safe_database_python() {
  "$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" exec \
    --pythonpath "$PLATFORM_ROOT" \
    -- "$QA_PYTHON" "$@"
}

reconcile_and_cleanup() {
  local marker="$1"
  local inventory="$2"
  safe_database_python "$TOOLS_DIR/platform_recover_live_user_qa.py" \
    --marker "$marker" \
    --inventory "$inventory" \
    --confirm recover-live-user-qa || return 1
  safe_database_python "$TOOLS_DIR/platform_cleanup_live_user_qa.py" \
    --marker "$marker" \
    --inventory "$inventory" \
    --confirm cleanup-live-user-qa
}

if (( $# == 2 )) && [[ "$1" == "recover-setup" ]]; then
  "${GUARD[@]}" remove-setup-state \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    --setup-dir "$2"
  echo "Interrupted pre-publication live QA setup state was removed exactly."
  exit 0
fi

if (( $# == 2 )); then
  RECOVERY_STATE="$2"
  MARKER="$(
    "${GUARD[@]}" validate-recovery \
      --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
      --state-dir "$RECOVERY_STATE"
  )"
  "${GUARD[@]}" merge-browser-inventory \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    --state-dir "$RECOVERY_STATE"
  "${GUARD[@]}" remove-browser-gate --state-dir "$RECOVERY_STATE"
  reconcile_and_cleanup "$MARKER" "$RECOVERY_STATE/inventory.json"
  "${GUARD[@]}" remove-root-state \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    --state-dir "$RECOVERY_STATE"
  echo "Automated live-user QA recovery completed with exact cleanup and absence proof."
  exit 0
fi

"${GUARD[@]}" preflight \
  --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
  --mode automated
RUNTIME_CACHE="$(
  "${GUARD[@]}" prepare-runtime-cache \
    --platform-root "$PLATFORM_ROOT" \
    --commit "$SOURCE_COMMIT"
)"
RUNTIME_NODE="$RUNTIME_CACHE/node/bin/node"
CHROMIUM_SANDBOX="$(
  "${GUARD[@]}" sandbox-path --runtime-cache "$RUNTIME_CACHE"
)"

QA_STATE_DIR="$(
  "${GUARD[@]}" prepare-root-state \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE"
)"
INVENTORY_PATH="$QA_STATE_DIR/inventory.json"
SESSIONS_PATH="$QA_STATE_DIR/browser-sessions.json"
MARKER="$(
  "${GUARD[@]}" validate-recovery \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    --state-dir "$QA_STATE_DIR"
)"
BROWSER_GATE=""

cleanup() {
  local original_status=$?
  local cleanup_status=1
  local recovered_marker=""
  trap - EXIT INT TERM HUP
  set +e
  if recovered_marker="$(
    "${GUARD[@]}" validate-recovery \
      --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
      --state-dir "$QA_STATE_DIR"
  )"; then
    MARKER="$recovered_marker"
    if "${GUARD[@]}" merge-browser-inventory \
      --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
      --state-dir "$QA_STATE_DIR" \
      && "${GUARD[@]}" remove-browser-gate --state-dir "$QA_STATE_DIR" \
      && reconcile_and_cleanup "$MARKER" "$INVENTORY_PATH"; then
      if "${GUARD[@]}" remove-root-state \
        --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
        --state-dir "$QA_STATE_DIR"; then
        cleanup_status=0
      fi
    fi
  else
    echo "Live-user QA exact inventory could not be validated; recovery state is retained." >&2
  fi
  if (( cleanup_status != 0 )); then
    echo "Live-user QA recovery state retained: $QA_STATE_DIR" >&2
    echo "Run this exact recovery command after resolving the failure:" >&2
    printf '  PLATFORM_APP_DIR=/opt/oldsparky/platform PLATFORM_LIVE_CSP_QA_BUNDLE=%q %q recover %q\n' \
      "$PLATFORM_LIVE_CSP_QA_BUNDLE" "$SCRIPT_PATH" "$QA_STATE_DIR" >&2
  fi
  if (( original_status != 0 )); then
    exit "$original_status"
  fi
  exit "$cleanup_status"
}
trap cleanup EXIT INT TERM HUP

REQUESTED_QA_MARKER="${PLATFORM_LIVE_USER_QA_MARKER:-}"
if [[ -n "$REQUESTED_QA_MARKER" && "$REQUESTED_QA_MARKER" != "$MARKER" ]]; then
  echo "PLATFORM_LIVE_USER_QA_MARKER does not match the provisioned bundle." >&2
  exit 1
fi
SESSION_MARKER="$(
  safe_database_python "$TOOLS_DIR/platform_provision_live_csp_qa.py" \
    prepare-browser-sessions \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    --inventory-path "$INVENTORY_PATH" \
    --sessions-path "$SESSIONS_PATH"
)"
if [[ "$SESSION_MARKER" != "$MARKER" ]]; then
  echo "Browser session fixture marker does not match the inventory." >&2
  exit 1
fi

BROWSER_GATE="$(
  "${GUARD[@]}" prepare-browser-gate \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    --state-dir "$QA_STATE_DIR"
)"
LIVE_QA_UID="$(/usr/bin/id -u oldsparky-liveqa)"
LIVE_QA_GID="$(/usr/bin/id -g oldsparky-liveqa)"
if [[ "$LIVE_QA_UID" == "0" || "$LIVE_QA_GID" == "0" ]]; then
  echo "Dedicated live QA identity must be unprivileged." >&2
  exit 1
fi
/usr/bin/systemd-run \
  --no-ask-password \
  --quiet \
  --wait \
  --collect \
  --pipe \
  --service-type=exec \
  --expand-environment=no \
  --unit=oldsparky-liveqa-browser.service \
  --uid="$LIVE_QA_UID" \
  --gid="$LIVE_QA_GID" \
  --working-directory="$RUNTIME_CACHE/web" \
  --property=KillMode=control-group \
  --property=Restart=no \
  --property=RuntimeMaxSec=30min \
  --property=SendSIGKILL=yes \
  --property=TimeoutStopSec=5s \
  --property=UMask=0077 \
  -- \
  /usr/bin/env -i \
    CHROME_DEVEL_SANDBOX="$CHROMIUM_SANDBOX" \
    HOME="$BROWSER_GATE/home" \
    LANG=C.UTF-8 \
    PATH="$RUNTIME_CACHE/node/bin:/usr/bin:/bin" \
    PLATFORM_LIVE_EXPECTED_ORIGIN="$EXPECTED_LIVE_ORIGIN" \
    PLATFORM_LIVE_USER_QA=1 \
    PLATFORM_LIVE_USER_QA_INVENTORY="$BROWSER_GATE/inventory.json" \
    PLATFORM_LIVE_USER_QA_MARKER="$MARKER" \
    PLATFORM_LIVE_USER_QA_SESSIONS="$BROWSER_GATE/browser-sessions.json" \
    PLATFORM_LIVE_USER_QA_UID="$LIVE_QA_UID" \
    PLATFORM_QA_BROWSER_GATE_DIR="$BROWSER_GATE" \
    PLAYWRIGHT_BROWSERS_PATH="$RUNTIME_CACHE/browsers" \
    PLAYWRIGHT_LIVE_BASE_URL="$EXPECTED_LIVE_ORIGIN" \
    TMPDIR="$BROWSER_GATE/tmp" \
    XDG_CACHE_HOME="$BROWSER_GATE/home/.cache" \
    "$RUNTIME_NODE" \
      "$RUNTIME_CACHE/web/node_modules/@playwright/test/cli.js" \
      test \
      --config="$RUNTIME_CACHE/web/playwright.live.config.ts" \
      "$RUNTIME_CACHE/web/tests/smoke/live-user-journey.spec.ts" \
      --project=live-desktop
