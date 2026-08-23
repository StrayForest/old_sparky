#!/usr/bin/env bash
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
ORIGINAL_ARGS=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || { echo "--app-dir requires a path." >&2; exit 1; }
      APP_DIR="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

if [[ "$APP_DIR" != /* || "$APP_DIR" == "/" ]]; then
  echo "Recovery application directory must be an absolute non-root path." >&2
  exit 1
fi

SHARED_DIR="$APP_DIR/shared"
RECOVERY_DIR="$SHARED_DIR/.release-recovery"
if [[ ! -d "$SHARED_DIR" || -L "$SHARED_DIR" || "$(stat -c %u:%a "$SHARED_DIR")" != "0:755" ]]; then
  echo "Recovery shared directory is missing or unsafe: $SHARED_DIR" >&2
  exit 1
fi
if [[ ! -d "$RECOVERY_DIR" || -L "$RECOVERY_DIR" || "$(stat -c %u:%a "$RECOVERY_DIR")" != "0:755" ]]; then
  echo "Stable recovery bundle directory is missing or unsafe: $RECOVERY_DIR" >&2
  exit 1
fi

RECOVERY_TOOL="$RECOVERY_DIR/platform_release_rollback.sh"
if [[ ! -f "$RECOVERY_TOOL" || -L "$RECOVERY_TOOL" || ! -x "$RECOVERY_TOOL" \
  || "$(stat -c %u:%a "$RECOVERY_TOOL")" != "0:755" ]]; then
  echo "Stable release recovery bundle is unavailable: $RECOVERY_TOOL" >&2
  exit 1
fi

exec "$RECOVERY_TOOL" "${ORIGINAL_ARGS[@]}"
