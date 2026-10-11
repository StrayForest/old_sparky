#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "$TOOLS_DIR/../apps/platform_web" && pwd)"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/oldsparky-web-hermetic.XXXXXX")"
CAPTURE_404_DIAGNOSTICS="${PLATFORM_WEB_404_DIAGNOSTICS:-}"
DIAGNOSTIC_DIR=""
DIAGNOSTIC_FILE=""
unset PLATFORM_WEB_404_DIAGNOSTICS

if [[ "$CAPTURE_404_DIAGNOSTICS" == "1" ]]; then
  DIAGNOSTIC_DIR="$(mktemp -d "${TMPDIR:-/tmp}/oldsparky-web-404-diagnostic.XXXXXX")"
  DIAGNOSTIC_FILE="$DIAGNOSTIC_DIR/next-server-output.log"
  (umask 077 && : > "$DIAGNOSTIC_FILE")
  chmod 600 "$DIAGNOSTIC_FILE"
  export PLATFORM_WEB_404_DIAGNOSTIC_FILE="$DIAGNOSTIC_FILE"
fi

cleanup() {
  local status=$?
  trap - EXIT
  if (( status != 0 )) && [[ -n "$DIAGNOSTIC_FILE" && -f "$DIAGNOSTIC_FILE" ]]; then
    mkdir -p "$WEB_ROOT/playwright-report"
    cp -- "$DIAGNOSTIC_FILE" "$WEB_ROOT/playwright-report/ssr-server-output.log" 2>/dev/null || true
    chmod 600 "$WEB_ROOT/playwright-report/ssr-server-output.log" 2>/dev/null || true
  fi
  rm -rf -- "$BUILD_DIR"
  if [[ -n "$DIAGNOSTIC_DIR" ]]; then
    rm -rf -- "$DIAGNOSTIC_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT

cd "$WEB_ROOT"
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
if [[ "$CAPTURE_404_DIAGNOSTICS" == "1" ]]; then
  PLATFORM_WEB_404_DIAGNOSTICS=1 \
  PLATFORM_WEB_404_DIAGNOSTIC_FILE="$DIAGNOSTIC_FILE" \
  PLAYWRIGHT_BROWSERS_PATH="$BUILD_DIR/browsers" \
  PLATFORM_WEB_HERMETIC_BUILD_DIR="$BUILD_DIR" \
    "$TOOLS_DIR/platform_web_npm.sh" run test:smoke
else
  PLAYWRIGHT_BROWSERS_PATH="$BUILD_DIR/browsers" \
  PLATFORM_WEB_HERMETIC_BUILD_DIR="$BUILD_DIR" \
    "$TOOLS_DIR/platform_web_npm.sh" run test:smoke
fi
PLAYWRIGHT_BROWSERS_PATH="$BUILD_DIR/browsers" \
PLATFORM_WEB_HERMETIC_BUILD_DIR="$BUILD_DIR" \
  "$TOOLS_DIR/platform_web_npm.sh" run test:participant-progressive
