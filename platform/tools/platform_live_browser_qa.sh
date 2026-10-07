#!/usr/bin/env bash
set +x
set -euo pipefail

TRUSTED_INSTALL_ROOT="${PLATFORM_LIVE_QA_INSTALL_ROOT:-}"
TRUSTED_MODE=0
if [[ -n "$TRUSTED_INSTALL_ROOT" ]]; then
  TRUSTED_MODE=1
  [[ "$TRUSTED_INSTALL_ROOT" =~ ^/root/\.oldsparky/liveqa/releases/[0-9a-f]{40}$ ]] \
    || { echo "Trusted live-QA install root is invalid." >&2; exit 1; }
  [[ "${PLATFORM_LIVE_QA_TARGET_SHA:-}" == "${TRUSTED_INSTALL_ROOT##*/}" ]] \
    || { echo "Trusted live-QA target SHA does not match its install root." >&2; exit 1; }
  PLATFORM_ROOT="$TRUSTED_INSTALL_ROOT/platform"
else
  TRUSTED_REPO_ROOT="/root/old_sparky"
  PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
fi
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_live_browser_qa.sh"
SYSTEM_PYTHON="/usr/bin/python3.12"

if ! { (( $# == 1 )) && [[ "$1" == "public" ]]; } \
  && ! { (( $# == 2 )) && [[ "$1" == "recover" && "$2" == /* ]]; }; then
  echo "Usage: $0 public | $0 recover /run/oldsparky-liveqa/public-live-qa.<suffix>" >&2
  exit 2
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "Production browser QA supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Production browser QA must run from the fixed root-controlled runtime." >&2
  exit 1
fi
if [[ "${PLATFORM_APP_DIR:-}" != "/opt/oldsparky/platform" ]]; then
  echo "Production browser QA requires the fixed production runtime contour." >&2
  exit 1
fi
: "${PLATFORM_LIVE_CSP_QA_BUNDLE:?PLATFORM_LIVE_CSP_QA_BUNDLE must point to the root-only CSP QA bundle}"
if [[ "$PLATFORM_LIVE_CSP_QA_BUNDLE" != /* ]]; then
  echo "PLATFORM_LIVE_CSP_QA_BUNDLE must be an absolute path." >&2
  exit 1
fi
if [[ -n "${PLATFORM_LIVE_CSP_ALLOW_LOOPBACK:-}" ]]; then
  echo "Production browser QA does not permit loopback mode." >&2
  exit 1
fi

GUARD=("$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_live_qa_guard.py")
if [[ -z "${PLATFORM_LIVE_QA_LOCK_FD:-}" ]]; then
  LOCK_COMMAND="locked-exec"
  if (( $# == 2 )) && [[ "$1" == "recover" ]]; then
    LOCK_COMMAND="recovery-locked-exec"
  fi
  exec "${GUARD[@]}" "$LOCK_COMMAND" \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
    -- "$SCRIPT_PATH" "$@"
fi
"${GUARD[@]}" assert-lock \
  --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
  --fd "$PLATFORM_LIVE_QA_LOCK_FD"
if (( TRUSTED_MODE == 1 )); then
  SOURCE_COMMIT="${PLATFORM_LIVE_QA_TARGET_SHA}"
else
  SOURCE_COMMIT="$("${GUARD[@]}" verify-provenance \
    --platform-root "$PLATFORM_ROOT" \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE")"
fi
if (( $# == 2 )); then
  "${GUARD[@]}" remove-public-browser-gate --gate "$2"
  echo "Interrupted public browser gate was removed exactly."
  exit 0
fi
"${GUARD[@]}" preflight \
  --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
  --mode automated
if (( TRUSTED_MODE == 1 )); then
  "$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_safe_env_exec.py" validate-runtime
fi
EXPECTED_LIVE_ORIGIN="$(
  "$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_safe_env_exec.py" \
    print-public-value PLATFORM_WEB_ORIGIN
)"
EXPECTED_ENVIRONMENT="$(
  "$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_safe_env_exec.py" \
    print-public-value PLATFORM_ENVIRONMENT
)"
if [[ "$EXPECTED_ENVIRONMENT" != "production" \
  || "$EXPECTED_LIVE_ORIGIN" != "https://old-sparky.com" \
  || -n "${PLAYWRIGHT_LIVE_BASE_URL:-}" \
  && "$PLAYWRIGHT_LIVE_BASE_URL" != "$EXPECTED_LIVE_ORIGIN" ]]; then
  echo "Production browser QA requires the canonical production origin." >&2
  exit 1
fi
if (( TRUSTED_MODE == 1 )); then
  RUNTIME_ENGINE_SOURCE="$("${GUARD[@]}" runtime-root)"
  RUNTIME_SUITE_SOURCE="$TRUSTED_INSTALL_ROOT/runtime"
else
  RUNTIME_ENGINE_SOURCE="$("${GUARD[@]}" prepare-runtime-cache \
    --platform-root "$PLATFORM_ROOT" \
    --commit "$SOURCE_COMMIT")"
  RUNTIME_SUITE_SOURCE="$RUNTIME_ENGINE_SOURCE"
fi
RUNTIME_CACHE="$RUNTIME_ENGINE_SOURCE"
RUNTIME_SUITE="$RUNTIME_SUITE_SOURCE"
RUNTIME_NODE="$RUNTIME_CACHE/node/bin/node"
if (( TRUSTED_MODE == 1 )); then
  CHROMIUM_SANDBOX="$RUNTIME_CACHE/browsers/chromium-1228/chrome-linux64/chrome_sandbox"
else
  CHROMIUM_SANDBOX="$(
    "${GUARD[@]}" sandbox-path --runtime-cache "$RUNTIME_CACHE"
  )"
fi
BROWSER_GATE="$("${GUARD[@]}" prepare-public-browser-gate)"
LIVE_QA_UID="$(/usr/bin/id -u oldsparky-liveqa)"
LIVE_QA_GID="$(/usr/bin/id -g oldsparky-liveqa)"
RUNTIME_MOUNTS_READY=0
RUNTIME_MOUNT_SOURCE_SHA=""
RUNTIME_MOUNT_PROVIDER_SHA=""
RUNTIME_MOUNT_SUFFIX=""
RUNTIME_MOUNT_SUITE_IDENTITY=""
RUNTIME_MOUNT_ENGINE_IDENTITY=""

cleanup_gate() {
  local original_status=$?
  local cleanup_status=0
  local gate_cleanup_status=0
  trap - EXIT INT TERM HUP
  set +e
  if (( RUNTIME_MOUNTS_READY == 1 )); then
    "${GUARD[@]}" cleanup-runtime-mounts \
      --source-sha "$RUNTIME_MOUNT_SOURCE_SHA" \
      --provider-sha "$RUNTIME_MOUNT_PROVIDER_SHA" \
      --suffix "$RUNTIME_MOUNT_SUFFIX" \
      --suite-identity "$RUNTIME_MOUNT_SUITE_IDENTITY" \
      --engine-identity "$RUNTIME_MOUNT_ENGINE_IDENTITY"
    cleanup_status=$?
    if (( cleanup_status != 0 )); then
      echo "Validated runtime mount targets retained for recovery." >&2
    fi
  fi
  "${GUARD[@]}" remove-public-browser-gate --gate "$BROWSER_GATE"
  gate_cleanup_status=$?
  if (( gate_cleanup_status != 0 )); then
    cleanup_status=$gate_cleanup_status
    echo "Public browser QA recovery gate retained: $BROWSER_GATE" >&2
    echo "Run this exact recovery command after resolving the failure:" >&2
    printf '  PLATFORM_APP_DIR=/opt/oldsparky/platform PLATFORM_LIVE_CSP_QA_BUNDLE=%q %q recover %q\n' \
      "$PLATFORM_LIVE_CSP_QA_BUNDLE" "$SCRIPT_PATH" "$BROWSER_GATE" >&2
  fi
  if (( original_status != 0 )); then
    exit "$original_status"
  fi
  exit "$cleanup_status"
}
trap cleanup_gate EXIT INT TERM HUP

SYSTEMD_BIND_ARGS=()
if (( TRUSTED_MODE == 1 )); then
  IFS=$'\t' read -r \
    RUNTIME_MOUNT_SOURCE_SHA \
    RUNTIME_MOUNT_PROVIDER_SHA \
    RUNTIME_MOUNT_SUFFIX \
    RUNTIME_MOUNT_SUITE_IDENTITY \
    RUNTIME_MOUNT_ENGINE_IDENTITY \
    < <("${GUARD[@]}" prepare-runtime-mounts --gate-path "$BROWSER_GATE")
  RUNTIME_MOUNTS_READY=1
  [[ "$RUNTIME_MOUNT_SOURCE_SHA" == "$SOURCE_COMMIT" \
    && "$RUNTIME_MOUNT_PROVIDER_SHA" =~ ^[0-9a-f]{40}$ \
    && "$RUNTIME_MOUNT_SUFFIX" =~ ^[a-z0-9_]{8}$ \
    && "$RUNTIME_MOUNT_SUITE_IDENTITY" =~ ^[0-9]+:[0-9]+$ \
    && "$RUNTIME_MOUNT_ENGINE_IDENTITY" =~ ^[0-9]+:[0-9]+$ ]] \
    || { echo "Validated live-QA runtime mapping identity is invalid." >&2; exit 1; }
  RUNTIME_MOUNT_SUITE_TARGET="/var/lib/oldsparky-liveqa/runtime-suite-${RUNTIME_MOUNT_SOURCE_SHA}-${RUNTIME_MOUNT_SUFFIX}"
  RUNTIME_MOUNT_ENGINE_TARGET="/var/lib/oldsparky-liveqa/runtime-engine-${RUNTIME_MOUNT_PROVIDER_SHA}-${RUNTIME_MOUNT_SUFFIX}"
  RUNTIME_SUITE="$RUNTIME_MOUNT_SUITE_TARGET"
  RUNTIME_CACHE="$RUNTIME_MOUNT_ENGINE_TARGET"
  RUNTIME_NODE="$RUNTIME_CACHE/node/bin/node"
  CHROMIUM_SANDBOX="$RUNTIME_CACHE/browsers/chromium-1228/chrome-linux64/chrome_sandbox"
  SYSTEMD_BIND_ARGS+=(
    "--property=BindReadOnlyPaths=$RUNTIME_SUITE_SOURCE:$RUNTIME_MOUNT_SUITE_TARGET"
    "--property=BindReadOnlyPaths=$RUNTIME_ENGINE_SOURCE:$RUNTIME_MOUNT_ENGINE_TARGET"
  )
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
  --working-directory="$RUNTIME_SUITE/web" \
  --property=KillMode=control-group \
  --property=Restart=no \
  --property=RuntimeMaxSec=30min \
  --property=SendSIGKILL=yes \
  --property=TimeoutStopSec=5s \
  --property=UMask=0077 \
  "${SYSTEMD_BIND_ARGS[@]}" \
  -- \
  /usr/bin/env -i \
    CHROME_DEVEL_SANDBOX="$CHROMIUM_SANDBOX" \
    HOME="$BROWSER_GATE/home" \
    LANG=C.UTF-8 \
    NODE_PATH="$RUNTIME_CACHE/web/node_modules" \
    PATH="$RUNTIME_CACHE/node/bin:/usr/bin:/bin" \
    PLATFORM_LIVE_EXPECTED_ORIGIN="$EXPECTED_LIVE_ORIGIN" \
    PLATFORM_LIVE_USER_QA_UID="$LIVE_QA_UID" \
    PLATFORM_QA_BROWSER_GATE_DIR="$BROWSER_GATE" \
    PLAYWRIGHT_BROWSERS_PATH="$RUNTIME_CACHE/browsers" \
    PLAYWRIGHT_LIVE_BASE_URL="$EXPECTED_LIVE_ORIGIN" \
    TMPDIR="$BROWSER_GATE/tmp" \
    XDG_CACHE_HOME="$BROWSER_GATE/home/.cache" \
    "$RUNTIME_NODE" \
      "$RUNTIME_CACHE/web/node_modules/@playwright/test/cli.js" \
      test \
      --config="$RUNTIME_SUITE/web/playwright.live.config.ts" \
      "$RUNTIME_SUITE/web/tests/smoke/live-launch.spec.ts"

printf 'LIVE_BROWSER_QA_SUCCESS source_commit=%s\n' "$SOURCE_COMMIT"
