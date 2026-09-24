#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1

# This fixed entrypoint is the only shell program allowed to cross from the
# secret-bearing workflow into the immutable, release-bound live-QA payload.
# The dispatcher performs the complete active-generation, bundle and mailbox
# validation before it execs the payload supervisor.
TRUSTED_ROOT="/root/.oldsparky/liveqa"
DISPATCHER="$TRUSTED_ROOT/platform_live_user_qa_dispatch.py"
BUNDLE="$TRUSTED_ROOT/csp-live-qa.json"
TARGET_SHA="${PLATFORM_LIVE_QA_TARGET_SHA:-}"
RELEASE_LOCK_EXEC="$TRUSTED_ROOT/platform_release_lock_exec.sh"

if [[ "$EUID" -ne 0 || ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Trusted live-user QA requires root and an exact target SHA." >&2
  exit 1
fi
if [[ ! -f "$DISPATCHER" || -L "$DISPATCHER" || ! -x "$DISPATCHER" ]]; then
  echo "Trusted live-QA dispatcher is unavailable." >&2
  exit 1
fi
if [[ ! -f "$RELEASE_LOCK_EXEC" || -L "$RELEASE_LOCK_EXEC" || ! -x "$RELEASE_LOCK_EXEC" ]]; then
  echo "Trusted live-QA release lock guard is unavailable." >&2
  exit 1
fi
if [[ "${PLATFORM_LIVE_CSP_QA_BUNDLE:-$BUNDLE}" != "$BUNDLE" ]]; then
  echo "Trusted live-user QA requires the fixed CSP QA bundle." >&2
  exit 1
fi

# Verify the active generation, root entrypoints and complete payload before
# opening the release lock.  The verifier itself is root-installed and
# digest-bound; no candidate/current/tools helper is crossed here.
/usr/bin/python3.12 -I -B "$DISPATCHER" verify "$TARGET_SHA"

# Hold the canonical release lock for the complete credential-bearing QA
# contour, including its exact cleanup/recovery work.  The guard validates
# that this active release names TARGET_SHA before entering the dispatcher.
exec "$RELEASE_LOCK_EXEC" --expected-sha "$TARGET_SHA" -- \
  /usr/bin/python3.12 -I -B "$DISPATCHER" run "$TARGET_SHA" "$@"
