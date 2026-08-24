#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

# Public rollback entrypoint. The large recovery implementation is retained in
# platform_release_rollback_impl.sh; this guard keeps operator semantics
# fail-closed without rewriting the proven crash-recovery state machine.

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
DRY_RUN=0
HELP_ONLY=0
ARGS=("$@")

for ((index = 0; index < ${#ARGS[@]}; index++)); do
  argument="${ARGS[$index]}"
  case "$argument" in
    --app-dir)
      if (( index + 1 >= ${#ARGS[@]} )); then
        echo "--app-dir requires a path." >&2
        exit 1
      fi
      APP_DIR="${ARGS[$((index + 1))]}"
      ((index += 1))
      ;;
    --no-restart)
      cat >&2 <<'EOF'
Refusing rollback without restart/readiness/smoke.
A production rollback is complete only after the target services restart and
pass smoke checks. Use the internal runtime-restore prepare mode only inside a
durable recovery transaction; --no-restart is not an operator rollback mode.
EOF
      exit 2
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --help|-h)
      HELP_ONLY=1
      ;;
  esac
done

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
IMPLEMENTATION="$TOOLS_DIR/platform_release_rollback_impl.sh"
if [[ ! -f "$IMPLEMENTATION" || -L "$IMPLEMENTATION" ]]; then
  echo "Rollback implementation is missing or unsafe: $IMPLEMENTATION" >&2
  exit 1
fi

# Before the implementation can switch current, make the implementation itself
# durable beside the existing recovery shim/bundle. The implementation's
# prepare_recovery_bundle() refreshes the public wrapper and companion tools but
# intentionally leaves this additional internal file in place.
if [[ "$DRY_RUN" -eq 0 && "$HELP_ONLY" -eq 0 && "$EUID" -eq 0 ]]; then
  if [[ "$APP_DIR" != /* || "$APP_DIR" == "/" ]]; then
    echo "Application directory must be an absolute non-root path." >&2
    exit 1
  fi
  RESOLVED_APP_DIR="$(readlink -f "$APP_DIR" 2>/dev/null || true)"
  if [[ -z "$RESOLVED_APP_DIR" || ! -d "$RESOLVED_APP_DIR" || -L "$APP_DIR" ]]; then
    echo "Application directory is missing or unsafe: $APP_DIR" >&2
    exit 1
  fi
  SHARED_DIR="$RESOLVED_APP_DIR/shared"
  if [[ ! -d "$SHARED_DIR" || -L "$SHARED_DIR" ]]; then
    echo "Shared release directory is missing or unsafe: $SHARED_DIR" >&2
    exit 1
  fi
  SHARED_UID="$(stat -c %u "$SHARED_DIR")"
  SHARED_MODE="$(stat -c %a "$SHARED_DIR")"
  if [[ "$SHARED_UID" != "0" || $((8#$SHARED_MODE & 8#022)) -ne 0 ]]; then
    echo "Shared release directory ownership or permissions are unsafe." >&2
    exit 1
  fi
  RECOVERY_DIR="$SHARED_DIR/.release-recovery"
  if [[ -e "$RECOVERY_DIR" || -L "$RECOVERY_DIR" ]]; then
    if [[ ! -d "$RECOVERY_DIR" || -L "$RECOVERY_DIR" \
      || "$(stat -c %u "$RECOVERY_DIR")" != "0" \
      || $((8#$(stat -c %a "$RECOVERY_DIR") & 8#022)) -ne 0 ]]; then
      echo "Stable recovery bundle is unsafe: $RECOVERY_DIR" >&2
      exit 1
    fi
  else
    install -d -o root -g root -m 0755 "$RECOVERY_DIR"
  fi
  TEMPORARY="$(mktemp "$RECOVERY_DIR/.platform_release_rollback_impl.sh.XXXXXX")"
  trap 'rm -f -- "${TEMPORARY:-}"' EXIT
  install -o root -g root -m 0755 "$IMPLEMENTATION" "$TEMPORARY"
  mv -T -- "$TEMPORARY" "$RECOVERY_DIR/platform_release_rollback_impl.sh"
  TEMPORARY=""
  trap - EXIT
fi

exec /bin/bash "$IMPLEMENTATION" "$@"
