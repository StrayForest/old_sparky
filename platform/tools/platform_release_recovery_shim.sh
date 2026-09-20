#!/usr/bin/env bash
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
public_status() {
  local status="$1"
  local class="$2"
  printf 'RELEASE_RECOVERY_SHIM schema=1 status=%s class=%s release_slug=unavailable\n' \
    "$status" "$class"
}
ORIGINAL_ARGS=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      APP_DIR="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

if [[ "$APP_DIR" != /* || "$APP_DIR" == "/" ]]; then
  public_status failed argument >&2
  exit 1
fi

SHARED_DIR="$APP_DIR/shared"
RECOVERY_DIR="$SHARED_DIR/.release-recovery"
if [[ ! -d "$SHARED_DIR" || -L "$SHARED_DIR" || "$(stat -c %u:%a "$SHARED_DIR")" != "0:755" ]]; then
  public_status failed layout >&2
  exit 1
fi
if [[ ! -d "$RECOVERY_DIR" || -L "$RECOVERY_DIR" || "$(stat -c %u:%a "$RECOVERY_DIR")" != "0:755" ]]; then
  public_status failed layout >&2
  exit 1
fi

RECOVERY_TOOL="$RECOVERY_DIR/platform_release_rollback.sh"
if [[ ! -f "$RECOVERY_TOOL" || -L "$RECOVERY_TOOL" || ! -x "$RECOVERY_TOOL" \
  || "$(stat -c %u:%a "$RECOVERY_TOOL")" != "0:755" ]]; then
  public_status failed recovery >&2
  exit 1
fi

LOCK_HELPER="$RECOVERY_DIR/platform_release_lock.sh"
if [[ ! -f "$LOCK_HELPER" || -L "$LOCK_HELPER" ]]; then
  public_status failed lock >&2
  exit 3
fi
# The stable recovery bundle is also a lock entrypoint.  Keep its descriptor
# open while delegating so rollback and runtime restore share one lock.
# shellcheck source=/dev/null
source "$LOCK_HELPER"
platform_release_lock_supervise "${ORIGINAL_ARGS[@]}" || {
  lock_status=$?
  if [[ "$lock_status" -eq "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE" ]]; then
    public_status failed lock >&2
    exit 3
  fi
  exit "$lock_status"
}
if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" != "1" ]]; then
  exit 0
fi
if ! platform_release_lock_open; then
  public_status failed lock >&2
  exit 3
fi
trap platform_release_lock_close EXIT

exec "$RECOVERY_TOOL" "${ORIGINAL_ARGS[@]}"
