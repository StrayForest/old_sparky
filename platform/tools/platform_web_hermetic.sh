#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "$TOOLS_DIR/../apps/platform_web" && pwd)"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/oldsparky-web-hermetic.XXXXXX")"
TIMING_OUTPUT="${PLATFORM_WEB_HERMETIC_TIMING_PATH:-}"
PHASE_LOG="$BUILD_DIR/phases.tsv"
SOURCE_TIMING="$BUILD_DIR/source-contract-timing.json"
SMOKE_TIMING="$BUILD_DIR/smoke-timing.json"
PARTICIPANT_TIMING="$BUILD_DIR/participant-timing.json"

run_phase() {
  local name="$1"
  shift
  local started finished status=0
  started="$(date +%s%3N)"
  "$@" || status=$?
  finished="$(date +%s%3N)"
  printf '%s\t%s\t%s\t%s\n' "$name" "$started" "$finished" "$status" >> "$PHASE_LOG"
  return "$status"
}

prepare_build_snapshot() {
  mkdir -p "$BUILD_DIR" \
    && cp -a .next/standalone "$BUILD_DIR/standalone" \
    && cp -a .next/static "$BUILD_DIR/static" \
    && cp -a public "$BUILD_DIR/public"
}

cleanup() {
  local status=$?
  local timing_status=0
  if [[ -n "$TIMING_OUTPUT" ]]; then
    /usr/bin/python3 "$TOOLS_DIR/platform_web_hermetic_timing.py" \
      --output "$TIMING_OUTPUT" \
      --phase-log "$PHASE_LOG" \
      --exit-status "$status" \
      --playwright-run "source-contract=$SOURCE_TIMING" \
      --playwright-run "smoke=$SMOKE_TIMING" \
      --playwright-run "participant=$PARTICIPANT_TIMING" \
      || timing_status=$?
  fi
  rm -rf -- "$BUILD_DIR"
  if (( status != 0 )); then
    return "$status"
  fi
  return "$timing_status"
}
trap cleanup EXIT

cd "$WEB_ROOT"
if [[ "${CI:-}" == "true" && "${GITHUB_ACTIONS:-}" == "true" && -z "$TIMING_OUTPUT" ]]; then
  echo "WEB_HERMETIC_TIMING status=failed reason=missing-output-path" >&2
  exit 2
fi
# Both isolated Playwright contours use the same localhost API destination in
# the standalone artifact.  The participant contour owns a fresh server on
# this port after smoke exits; no server or database state is shared.
run_phase build env \
  PLATFORM_API_BASE_URL="http://127.0.0.1:3199/api/v1" \
  "$TOOLS_DIR/platform_web_npm.sh" run build
run_phase snapshot prepare_build_snapshot

# Validate the executable project ownership before any browser test starts.
# This invokes Playwright's real --list output, so a config exclusion cannot
# silently change the responsive/request/source matrix.
run_phase matrix /usr/bin/python3 "$TOOLS_DIR/platform_web_hermetic_matrix.py" \
  --web-root "$WEB_ROOT"

# Provision a fresh per-run browser root from the checked-in Playwright
# revision and the pinned archive checksums.  Playwright must never fall back
# to a developer or CI user's global cache after cache cleanup.
run_phase browsers env PLAYWRIGHT_BROWSERS_PATH="$BUILD_DIR/browsers" \
  "$TOOLS_DIR/platform_web_hermetic_browsers.py" \
  --web-root "$WEB_ROOT" \
  --output "$BUILD_DIR/browsers"

# Source-contract assertions read repository code and have no browser/server
# dependency, so run them once in their dedicated runner.
run_phase source-contract env \
  PLATFORM_WEB_TEST_TIMING_PATH="$SOURCE_TIMING" \
  "$TOOLS_DIR/platform_web_npm.sh" run test:source-contract

# The smoke and participant contours remain separate Playwright runs with
# separate API server processes and server environments.  They consume the
# same immutable standalone build sequentially, which removes the proven
# duplicate Next build without sharing a running server or a database.
run_phase smoke env \
  PLAYWRIGHT_BROWSERS_PATH="$BUILD_DIR/browsers" \
  PLATFORM_WEB_HERMETIC_BUILD_DIR="$BUILD_DIR" \
  PLATFORM_WEB_TEST_TIMING_PATH="$SMOKE_TIMING" \
  "$TOOLS_DIR/platform_web_npm.sh" run test:smoke
run_phase participant env \
  PLAYWRIGHT_BROWSERS_PATH="$BUILD_DIR/browsers" \
  PLATFORM_WEB_HERMETIC_BUILD_DIR="$BUILD_DIR" \
  PLATFORM_WEB_TEST_TIMING_PATH="$PARTICIPANT_TIMING" \
  "$TOOLS_DIR/platform_web_npm.sh" run test:participant-progressive
