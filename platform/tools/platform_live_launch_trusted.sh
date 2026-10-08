#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1

# Fixed root-owned entrypoint for the legacy live-launch signal.  It shares the
# digest-bound generation with live-user QA but remains a separate signal: it
# may provision/rotate the CSP bundle and runs the public browser journey.
TRUSTED_ROOT="/root/.oldsparky/liveqa"
DISPATCHER="$TRUSTED_ROOT/platform_live_user_qa_dispatch.py"
RELEASE_LOCK_EXEC="$TRUSTED_ROOT/platform_release_lock_exec.sh"
APP_DIR="/opt/oldsparky/platform"
if (($# != 4 && $# != 6)); then
  echo "Trusted live-launch received an invalid argument count." >&2
  exit 2
fi
BASE_URL="$1"
PROVISION="$2"
MARKER="$3"
SOURCE_ARGUMENTS=()
if (($# == 4)); then
  RUNNER_SHA="$4"
else
  [[ "$4" == "--source-binding-base64" ]] || {
    echo "Trusted live-launch source-binding option is invalid." >&2
    exit 2
  }
  SOURCE_ARGUMENTS=("$4" "$5")
  RUNNER_SHA="$6"
fi
APP_TARGET_SHA="$RUNNER_SHA"

if [[ "$EUID" -ne 0 || ! "$RUNNER_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Trusted live-launch requires root and an exact target SHA." >&2
  exit 1
fi
if [[ "$BASE_URL" != "https://old-sparky.com" ]]; then
  echo "Trusted live-launch requires the canonical production origin." >&2
  exit 1
fi
case "$PROVISION" in
  true)
    [[ "$MARKER" =~ ^liveqa-[a-z0-9-]{6,56}$ ]] \
      || { echo "Provisioning requires a fresh liveqa marker." >&2; exit 2; }
    ;;
  false)
    [[ -z "$MARKER" ]] \
      || { echo "A marker is allowed only with provision=true." >&2; exit 2; }
    ;;
  *)
    echo "provision must be true or false." >&2
    exit 2
    ;;
esac
if [[ ! -f "$DISPATCHER" || -L "$DISPATCHER" || ! -x "$DISPATCHER" \
  || ! -f "$RELEASE_LOCK_EXEC" || -L "$RELEASE_LOCK_EXEC" \
  || ! -x "$RELEASE_LOCK_EXEC" ]]; then
  echo "Trusted live-launch generation entrypoint is unavailable." >&2
  exit 1
fi

# Resolve only the no-op binding accepted by the closed C2 handoff. The app
# dispatcher checks the complete tuple again after the release lock is held.
if ((${#SOURCE_ARGUMENTS[@]})); then
  binding_marker="$(/usr/bin/python3.12 -I -B \
    "$DISPATCHER" resolve "$RUNNER_SHA" "${SOURCE_ARGUMENTS[@]}")" || {
    echo "Trusted live-launch source binding is invalid." >&2
    exit 1
  }
  if [[ "$binding_marker" =~ ^LIVE_SOURCE_BINDING\ schema=1\ runner_sha=([0-9a-f]{40})\ app_target_sha=([0-9a-f]{40})$ ]] \
    && [[ "${BASH_REMATCH[1]}" == "$RUNNER_SHA" ]]; then
    APP_TARGET_SHA="${BASH_REMATCH[2]}"
  else
    echo "Trusted live-launch source binding response is invalid." >&2
    exit 1
  fi
fi
/usr/bin/python3.12 -I -B "$DISPATCHER" verify "$RUNNER_SHA" "${SOURCE_ARGUMENTS[@]}"

# The release lock covers the complete credential-bearing launch contour,
# including provisioning, browser execution and their exact cleanup paths.
exec "$RELEASE_LOCK_EXEC" --app-dir "$APP_DIR" --expected-sha "$APP_TARGET_SHA" -- \
  /usr/bin/python3.12 -I -B "$DISPATCHER" run-launch \
  "$RUNNER_SHA" "$BASE_URL" "$PROVISION" "$MARKER" "${SOURCE_ARGUMENTS[@]}"
