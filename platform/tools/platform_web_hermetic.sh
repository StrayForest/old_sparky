#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "$TOOLS_DIR/../apps/platform_web" && pwd)"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/oldsparky-web-hermetic.XXXXXX")"
RUN_ID="oldsparky-web-hermetic-${BASHPID}-$(date +%s%N)"
export PLATFORM_WEB_HERMETIC_RUN_ID="$RUN_ID"

cleanup() {
  local status=$?
  local cleanup_status=0
  trap - EXIT
  trap '' INT TERM HUP
  if "$TOOLS_DIR/platform_node.sh" \
    "$TOOLS_DIR/platform_web_hermetic_cleanup.cjs" cleanup "$RUN_ID"; then
    :
  else
    cleanup_status=$?
    if [[ "$status" -eq 0 ]]; then status="$cleanup_status"; fi
  fi
  if rm -rf -- "$BUILD_DIR"; then
    :
  else
    cleanup_status=$?
    if [[ "$status" -eq 0 ]]; then status="$cleanup_status"; fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

cd "$WEB_ROOT"
# Fail before an expensive build when another task owns one of the fixed local
# ports this contour uses. Cleanup remains scoped to the unique run ID above.
"$TOOLS_DIR/platform_node.sh" \
  "$TOOLS_DIR/platform_web_hermetic_cleanup.cjs" preflight \
  3198 3199 3100 3101
# Exercise the same process-ownership cleanup used by the signal/EXIT trap.
"$TOOLS_DIR/platform_node.sh" \
  "$WEB_ROOT/tests/support/web-hermetic-lifecycle-contract.mjs"
# The public live-QA count reporter is privacy-sensitive contract code.  Run
# its pure Node contract in the existing web-hermetic gate without starting a
# browser, server, API or database.
"$TOOLS_DIR/platform_node.sh" \
  "$WEB_ROOT/tests/support/live-count-reporter-contract.cjs"
# Both isolated Playwright contours use the same localhost API destination in
# the standalone artifact.  The participant contour owns a fresh server on
# this port after smoke exits; no server or database state is shared.
PLATFORM_API_BASE_URL="http://127.0.0.1:3199/api/v1" \
  "$TOOLS_DIR/platform_web_npm.sh" run build
mkdir -p "$BUILD_DIR"
cp -a .next/standalone "$BUILD_DIR/standalone"
cp -a .next/static "$BUILD_DIR/static"
cp -a public "$BUILD_DIR/public"

# Provision a fresh per-run browser root from the checked-in Playwright
# revision and the pinned archive checksums.  Playwright must never fall back
# to a developer or CI user's global cache after cache cleanup.
PLAYWRIGHT_BROWSERS_PATH="$BUILD_DIR/browsers" \
  "$TOOLS_DIR/platform_web_hermetic_browsers.py" \
  --web-root "$WEB_ROOT" \
  --output "$BUILD_DIR/browsers"

# Source-contract assertions read repository code and have no browser/server
# dependency, so run them once in their dedicated runner.
"$TOOLS_DIR/platform_web_npm.sh" run test:source-contract

# The smoke and participant contours remain separate Playwright runs with
# separate API server processes and server environments.  They consume the
# same immutable standalone build sequentially, which removes the proven
# duplicate Next build without sharing a running server or a database.
PLAYWRIGHT_BROWSERS_PATH="$BUILD_DIR/browsers" \
PLATFORM_WEB_HERMETIC_BUILD_DIR="$BUILD_DIR" \
  "$TOOLS_DIR/platform_web_npm.sh" run test:smoke
PLAYWRIGHT_BROWSERS_PATH="$BUILD_DIR/browsers" \
PLATFORM_WEB_HERMETIC_BUILD_DIR="$BUILD_DIR" \
  "$TOOLS_DIR/platform_web_npm.sh" run test:participant-progressive
