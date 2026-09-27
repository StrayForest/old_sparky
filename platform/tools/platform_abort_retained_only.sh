#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

# This file is installed in a content-addressed recovery generation and is the
# only fixed recovery entrypoint.  It deliberately never resolves tools from
# current, previous, a source checkout, or an application release.
APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || exit 2
      APP_DIR="$2"
      shift 2
      ;;
    *)
      exit 2
      ;;
  esac
done

[[ "$EUID" -eq 0 ]] || exit 1
[[ "$APP_DIR" == /* && "$APP_DIR" != "/" ]] || exit 2
GENERATION_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LOCK_HELPER="$GENERATION_DIR/platform_release_lock.sh"
BOOTSTRAP="$GENERATION_DIR/platform_recovery_bootstrap.py"
[[ -f "$LOCK_HELPER" && ! -L "$LOCK_HELPER" && -x "$LOCK_HELPER" ]] || exit 1
[[ -f "$BOOTSTRAP" && ! -L "$BOOTSTRAP" ]] || exit 1

# The pathname-form supervisor owns the canonical release lock.  No lock FD
# or ambient environment is accepted by the fixed Python driver.
exec "$LOCK_HELPER" --run /usr/bin/python3 -I "$BOOTSTRAP" \
  abort_retained_only --app-dir "$APP_DIR" --generation "$GENERATION_DIR"
