#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

# Fixed root-owned entrypoint for the legacy live-launch signal.  It shares the
# digest-bound generation with live-user QA but remains a separate signal: it
# may provision/rotate the CSP bundle and runs the public browser journey.
TRUSTED_ROOT="/root/.oldsparky/liveqa"
DISPATCHER="$TRUSTED_ROOT/platform_live_user_qa_dispatch.py"
RELEASE_LOCK_EXEC="$TRUSTED_ROOT/platform_release_lock_exec.sh"
APP_DIR="/opt/oldsparky/platform"
TARGET_SHA="${PLATFORM_LIVE_QA_TARGET_SHA:-}"
BASE_URL="${PLAYWRIGHT_LIVE_BASE_URL:-}"
PROVISION="${PLATFORM_LIVE_PROVISION:-}"
MARKER="${PLATFORM_LIVE_MARKER:-}"

if [[ "$EUID" -ne 0 || ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
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

# The digest verifier binds the active SHA, pointer, complete tree manifest and
# every root entrypoint before the canonical release lock is opened.
/usr/bin/python3.12 -I "$DISPATCHER" verify "$TARGET_SHA"

# The release lock covers the complete credential-bearing launch contour,
# including provisioning, browser execution and their exact cleanup paths.
exec "$RELEASE_LOCK_EXEC" --app-dir "$APP_DIR" --expected-sha "$TARGET_SHA" -- \
  /usr/bin/python3.12 -I "$DISPATCHER" run-launch \
  "$TARGET_SHA" "$BASE_URL" "$PROVISION" "$MARKER"
