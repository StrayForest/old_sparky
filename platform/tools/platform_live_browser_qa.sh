#!/usr/bin/env bash
set +x
set -euo pipefail

TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
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
  echo "Production browser QA must run from the fixed root-controlled checkout." >&2
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

GUARD=("$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_live_qa_guard.py")
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
SOURCE_COMMIT="$(
  "${GUARD[@]}" verify-provenance \
    --platform-root "$PLATFORM_ROOT" \
    --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE"
)"
if (( $# == 2 )); then
  "${GUARD[@]}" remove-public-browser-gate --gate "$2"
  echo "Interrupted public browser gate was removed exactly."
  exit 0
fi
"${GUARD[@]}" preflight \
  --bundle-path "$PLATFORM_LIVE_CSP_QA_BUNDLE" \
  --mode automated
EXPECTED_LIVE_ORIGIN="$(
  "$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" \
    print-public-value PLATFORM_WEB_ORIGIN
)"
EXPECTED_ENVIRONMENT="$(
  "$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" \
    print-public-value PLATFORM_ENVIRONMENT
)"
if [[ "$EXPECTED_ENVIRONMENT" != "production" \
  || "$EXPECTED_LIVE_ORIGIN" != "https://old-sparky.com" \
  || -n "${PLAYWRIGHT_LIVE_BASE_URL:-}" \
  && "$PLAYWRIGHT_LIVE_BASE_URL" != "$EXPECTED_LIVE_ORIGIN" ]]; then
  echo "Production browser QA requires the canonical production origin." >&2
  exit 1
fi
RUNTIME_CACHE="$(
  "${GUARD[@]}" prepare-runtime-cache \
    --platform-root "$PLATFORM_ROOT" \
    --commit "$SOURCE_COMMIT"
)"
RUNTIME_NODE="$RUNTIME_CACHE/node/bin/node"
CHROMIUM_SANDBOX="$(
  "${GUARD[@]}" sandbox-path --runtime-cache "$RUNTIME_CACHE"
)"
BROWSER_GATE="$("${GUARD[@]}" prepare-public-browser-gate)"
LIVE_QA_UID="$(/usr/bin/id -u oldsparky-liveqa)"
LIVE_QA_GID="$(/usr/bin/id -g oldsparky-liveqa)"

cleanup_gate() {
  local original_status=$?
  local cleanup_status=0
  trap - EXIT INT TERM HUP
  set +e
  "${GUARD[@]}" remove-public-browser-gate --gate "$BROWSER_GATE"
  cleanup_status=$?
  if (( cleanup_status != 0 )); then
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
      "$RUNTIME_CACHE/web/tests/smoke/live-launch.spec.ts"

printf 'LIVE_BROWSER_QA_SUCCESS source_commit=%s\n' "$SOURCE_COMMIT"
