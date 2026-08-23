#!/usr/bin/env bash
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
RESTART_AFTER=1
NO_RESTART_REQUESTED=0
DRY_RUN=0
RESTORE_VENV=1
RECOVER_PENDING=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      if [[ $# -lt 2 ]]; then
        echo "--app-dir requires a path." >&2
        exit 1
      fi
      APP_DIR="$2"
      shift 2
      ;;
    --no-restart)
      RESTART_AFTER=0
      NO_RESTART_REQUESTED=1
      shift
      ;;
    --skip-venv-restore)
      RESTORE_VENV=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --recover-pending)
      RECOVER_PENDING=1
      shift
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_release_rollback.sh [--app-dir <path>] [--no-restart] [--skip-venv-restore] [--dry-run]
       platform_release_rollback.sh --recover-pending [--app-dir <path>]

Switches /opt/oldsparky/platform/current back to /opt/oldsparky/platform/previous.
This does not reverse database migrations. By default, the exact shared Python
runtime retained by the current release is atomically restored with the pointer
switch. A release installed with --skip-python-deps instead carries a root-only
receipt; rollback verifies that the unchanged shared venv still exactly matches
that receipt before switching pointers. --skip-venv-restore requires manual
dependency compatibility review and must not bypass a failed receipt check.

If an install or rollback was interrupted, --recover-pending restores the exact
pre-operation pointers and venv from shared/.release-operation.json, removes an
unactivated install candidate, and exits. A rollback already in restart-pending
instead repeats the service restart and durably completes that same rollback.
The rollback path prepares shared/.release-recovery before switching current;
the previous release's rollback entrypoint delegates there if a second process
must recover after that pointer switch.
Retry the intended operation afterward only after pre-operation recovery.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  echo "Platform release rollback requires root." >&2
  exit 1
fi
if [[ "$APP_DIR" != /* ]]; then
  echo "Application directory must be an absolute path." >&2
  exit 1
fi
if [[ "$RECOVER_PENDING" -eq 1 \
  && ( "$DRY_RUN" -eq 1 || "$RESTORE_VENV" -eq 0 || "$NO_RESTART_REQUESTED" -eq 1 ) ]]; then
  echo "--recover-pending cannot be combined with rollback mode options." >&2
  exit 1
fi

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TRANSACTION_TOOL="$TOOLS_DIR/platform_release_transaction.py"
RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"
if [[ ! -d "$APP_DIR" || -L "$APP_DIR" ]]; then
  echo "Application directory is missing or unsafe: $APP_DIR" >&2
  exit 1
fi
APP_DIR="$(readlink -f "$APP_DIR")"
RELEASES_DIR="$APP_DIR/releases"
SHARED_DIR="$APP_DIR/shared"
SHARED_VENV_DIR="$SHARED_DIR/venv"
TRANSACTION_STATE="$SHARED_DIR/.release-operation.json"
RECOVERY_BUNDLE_DIR="$SHARED_DIR/.release-recovery"

for SAFE_DIR in "$APP_DIR" "$RELEASES_DIR" "$SHARED_DIR"; do
  if [[ ! -d "$SAFE_DIR" || -L "$SAFE_DIR" ]]; then
    echo "Rollback directory is missing or unsafe: $SAFE_DIR" >&2
    exit 1
  fi
  SAFE_UID="$(stat -c %u "$SAFE_DIR")"
  SAFE_MODE="$(stat -c %a "$SAFE_DIR")"
  if [[ "$SAFE_UID" != "0" || $((8#$SAFE_MODE & 8#022)) -ne 0 ]]; then
    echo "Rollback directory ownership or permissions are unsafe: $SAFE_DIR" >&2
    exit 1
  fi
done

exec {RELEASE_LOCK_FD}<"$SHARED_DIR"
if ! /usr/bin/flock -n "$RELEASE_LOCK_FD"; then
  echo "Another platform install or rollback operation holds the release lock." >&2
  exit 3
fi

transaction_json() {
  /usr/bin/python3 -I "$TRANSACTION_TOOL" status \
    --state "$TRANSACTION_STATE" --json
}

json_field() {
  local field="$1"
  /usr/bin/python3 -I -c \
    'import json,sys; value=json.load(sys.stdin)[sys.argv[1]]; print("" if value is None else value)' \
    "$field"
}

restore_release_runtime() {
  local release="$1"
  if [[ "$RESTART_AFTER" -eq 1 ]]; then
    "$RUNTIME_RESTORE_TOOL" \
      --app-dir "$APP_DIR" \
      --release "$release"
  else
    "$RUNTIME_RESTORE_TOOL" \
      --app-dir "$APP_DIR" \
      --release "$release" \
      --no-restart
  fi
}

recover_rollback_to_original() {
  local phase="$1"
  local original_current
  original_current="$(transaction_json | json_field current_before)"
  if [[ -z "$original_current" ]]; then
    original_current="$(transaction_json | json_field previous_before)"
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
    --retain \
    --state "$TRANSACTION_STATE"
  restore_release_runtime "$original_current"
  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
    --state "$TRANSACTION_STATE"
  echo "Rollback recovery restored the original release from phase: $phase." >&2
}

if [[ -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
  if [[ "$RECOVER_PENDING" -eq 0 ]]; then
    cat >&2 <<EOF
A pending platform release operation must be recovered before rollback.
Run:
  platform/tools/platform_release_rollback.sh --recover-pending --app-dir "$APP_DIR"
Then retry the intended install or rollback command.
EOF
    exit 3
  fi
  TRANSACTION_STATUS="$(
    /usr/bin/python3 -I "$TRANSACTION_TOOL" status --state "$TRANSACTION_STATE"
  )"
  PENDING_OPERATION="${TRANSACTION_STATUS%% *}"
  PENDING_PHASE="${TRANSACTION_STATUS#* }"
  if [[ "$PENDING_OPERATION" == "rollback" \
    && "$PENDING_PHASE" == "restart-pending" ]]; then
    trap '' HUP INT TERM
    PENDING_RELEASE="$(readlink -f "$APP_DIR/current")"
    "$RUNTIME_RESTORE_TOOL" \
      --app-dir "$APP_DIR" \
      --release "$PENDING_RELEASE"
    /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"
    trap - HUP INT TERM
    echo "Pending rollback service restart completed; the rollback remains active."
  elif [[ "$PENDING_OPERATION" == "rollback" \
    && "$PENDING_PHASE" == "rollback-runtime-pending" ]]; then
    trap '' HUP INT TERM
    recover_rollback_to_original "$PENDING_PHASE"
    trap - HUP INT TERM
  elif [[ "$PENDING_OPERATION" == "rollback" \
    && "$PENDING_PHASE" == "rollback-runtime-applied" ]]; then
    trap '' HUP INT TERM
    /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"
    trap - HUP INT TERM
    echo "Pending rollback receipt completed after runtime restoration."
  elif [[ "$PENDING_OPERATION" == "rollback" \
    && ( "$PENDING_PHASE" == "services-restarted" \
      || "$PENDING_PHASE" == "smoke-passed" ) ]]; then
    trap '' HUP INT TERM
    recover_rollback_to_original "$PENDING_PHASE"
    trap - HUP INT TERM
  else
    /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --state "$TRANSACTION_STATE"
    echo "Pending platform release operation recovered to its exact pre-operation state."
  fi
  exit 0
fi
if [[ "$RECOVER_PENDING" -eq 1 ]]; then
  echo "No pending platform release operation exists." >&2
  exit 1
fi

validate_release_pointer() {
  local pointer_name="$1"
  local pointer_path="$APP_DIR/$pointer_name"
  local target=""
  if [[ ! -L "$pointer_path" || "$(stat -c %u "$pointer_path")" != "0" ]]; then
    echo "Release pointer is missing or unsafe: $pointer_path" >&2
    return 1
  fi
  target="$(readlink -f "$pointer_path" 2>/dev/null || true)"
  if [[ -z "$target" || ! -d "$target" || -L "$target" \
    || "$(dirname "$target")" != "$RELEASES_DIR" \
    || ! "$(basename "$target")" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
    echo "Release pointer escapes the installed releases: $pointer_path" >&2
    return 1
  fi
  local target_uid target_mode
  target_uid="$(stat -c %u "$target")"
  target_mode="$(stat -c %a "$target")"
  if [[ "$target_uid" != "0" || $((8#$target_mode & 8#022)) -ne 0 ]]; then
    echo "Release target ownership or permissions are unsafe: $target" >&2
    return 1
  fi
  printf '%s\n' "$target"
}

CURRENT_TARGET="$(validate_release_pointer current)"
PREVIOUS_TARGET="$(validate_release_pointer previous)"
if [[ "$CURRENT_TARGET" == "$PREVIOUS_TARGET" ]]; then
  echo "Current and previous releases are identical; nothing to roll back." >&2
  exit 1
fi

VENV_ROLLBACK_DIR="$CURRENT_TARGET/.rollback"
VENV_ROLLBACK_SNAPSHOT_DIR="$VENV_ROLLBACK_DIR/shared-venv-before-install"
VENV_ROLLBACK_PREVIOUS_FILE="$VENV_ROLLBACK_DIR/previous-release"
VENV_ROLLBACK_TRANSITION_FILE="$VENV_ROLLBACK_DIR/venv-transition"
VENV_ROLLBACK_FREEZE_FILE="$VENV_ROLLBACK_DIR/shared-freeze.sha256"
VENV_RESTORE_MODE="skipped"

validate_root_directory() {
  local path="$1"
  local label="$2"
  if [[ ! -d "$path" || -L "$path" ]]; then
    echo "$label is missing or unsafe: $path" >&2
    return 1
  fi
  local owner mode
  owner="$(stat -c %u "$path")"
  mode="$(stat -c %a "$path")"
  if [[ "$owner" != "0" || $((8#$mode & 8#022)) -ne 0 ]]; then
    echo "$label ownership or permissions are unsafe: $path" >&2
    return 1
  fi
}

prepare_recovery_bundle() {
  if [[ -e "$RECOVERY_BUNDLE_DIR" || -L "$RECOVERY_BUNDLE_DIR" ]]; then
    validate_root_directory "$RECOVERY_BUNDLE_DIR" "Stable recovery bundle"
  else
    install -d -o root -g root -m 0755 "$RECOVERY_BUNDLE_DIR"
  fi

  local filename source temporary destination
  for filename in \
    platform_release_rollback.sh \
    platform_release_transaction.py \
    platform_release_restore_runtime.sh \
    platform_release_recovery_shim.sh; do
    source="$TOOLS_DIR/$filename"
    destination="$RECOVERY_BUNDLE_DIR/$filename"
    if [[ ! -f "$source" || -L "$source" ]]; then
      echo "Recovery bundle source is unavailable: $source" >&2
      return 1
    fi
    temporary="$(mktemp "$RECOVERY_BUNDLE_DIR/.$filename.XXXXXX")"
    install -o root -g root -m 0755 "$source" "$temporary"
    mv -T -- "$temporary" "$destination"
  done
}

install_recovery_shim() {
  local release="$1"
  local release_tools="$release/tools"
  local source="$RECOVERY_BUNDLE_DIR/platform_release_recovery_shim.sh"
  local destination="$release_tools/platform_release_rollback.sh"
  local temporary
  validate_root_directory "$release_tools" "Previous release tools"
  if [[ ! -f "$source" || -L "$source" || ! -x "$source" ]]; then
    echo "Stable recovery shim is unavailable: $source" >&2
    return 1
  fi
  if [[ -f "$destination" && ! -L "$destination" ]] && cmp -s "$source" "$destination"; then
    return 0
  fi
  temporary="$(mktemp "$release_tools/.platform_release_rollback.sh.recovery.XXXXXX")"
  install -o root -g root -m 0755 "$source" "$temporary"
  mv -T -- "$temporary" "$destination"
}

read_safe_record() {
  local path="$1"
  local label="$2"
  if [[ ! -f "$path" || -L "$path" ]]; then
    echo "$label is missing or unsafe." >&2
    return 1
  fi
  local owner links mode size lines value
  owner="$(stat -c %u "$path")"
  links="$(stat -c %h "$path")"
  mode="$(stat -c %a "$path")"
  size="$(stat -c %s "$path")"
  lines="$(wc -l <"$path")"
  if [[ "$owner" != "0" || "$links" != "1" || "$mode" != "600" \
    || "$size" -gt 4096 || "$lines" != "1" ]]; then
    echo "$label metadata is unsafe." >&2
    return 1
  fi
  IFS= read -r value <"$path"
  printf '%s\n' "$value"
}

if [[ -e "$SHARED_VENV_DIR" || -L "$SHARED_VENV_DIR" ]]; then
  validate_root_directory "$SHARED_VENV_DIR" "Shared venv"
  if [[ ! -x "$SHARED_VENV_DIR/bin/python" ]]; then
    echo "Shared venv Python is unavailable." >&2
    exit 1
  fi
fi

if [[ "$RESTORE_VENV" -eq 1 ]]; then
  validate_root_directory "$SHARED_VENV_DIR" "Shared venv"
  validate_root_directory "$VENV_ROLLBACK_DIR" "Rollback metadata directory"
  EXPECTED_PREVIOUS_TARGET="$(
    read_safe_record \
      "$VENV_ROLLBACK_PREVIOUS_FILE" \
      "Rollback expected-previous record"
  )"
  if [[ "$EXPECTED_PREVIOUS_TARGET" != "$PREVIOUS_TARGET" ]]; then
    cat >&2 <<EOF
Release rollback metadata does not match the previous release.
  receipt expects: $EXPECTED_PREVIOUS_TARGET
  previous target: $PREVIOUS_TARGET
Use --skip-venv-restore only after manually verifying exact dependency compatibility.
EOF
    exit 1
  fi
  VENV_TRANSITION_MODE="snapshot"
  if [[ -e "$VENV_ROLLBACK_TRANSITION_FILE" \
    || -L "$VENV_ROLLBACK_TRANSITION_FILE" ]]; then
    VENV_TRANSITION_MODE="$(
      read_safe_record \
        "$VENV_ROLLBACK_TRANSITION_FILE" \
        "Rollback venv transition record"
    )"
  fi
  case "$VENV_TRANSITION_MODE" in
    snapshot)
      validate_root_directory "$VENV_ROLLBACK_SNAPSHOT_DIR" "Rollback venv snapshot"
      if [[ ! -x "$VENV_ROLLBACK_SNAPSHOT_DIR/bin/python" ]]; then
        echo "Rollback venv snapshot Python is unavailable." >&2
        exit 1
      fi
      if [[ -e "$VENV_ROLLBACK_FREEZE_FILE" \
        || -L "$VENV_ROLLBACK_FREEZE_FILE" ]]; then
        echo "Snapshot rollback has an unexpected unchanged-freeze record." >&2
        exit 1
      fi
      VENV_RESTORE_MODE="atomic"
      ;;
    unchanged)
      if [[ -e "$VENV_ROLLBACK_SNAPSHOT_DIR" \
        || -L "$VENV_ROLLBACK_SNAPSHOT_DIR" ]]; then
        echo "Unchanged-venv rollback has an unexpected snapshot." >&2
        exit 1
      fi
      EXPECTED_FREEZE_DIGEST="$(
        read_safe_record \
          "$VENV_ROLLBACK_FREEZE_FILE" \
          "Rollback unchanged-freeze record"
      )"
      if [[ ! "$EXPECTED_FREEZE_DIGEST" =~ ^[0-9a-f]{64}$ ]]; then
        echo "Rollback unchanged-freeze digest is invalid." >&2
        exit 1
      fi
      CURRENT_FREEZE="$CURRENT_TARGET/requirements-platform.freeze.txt"
      if [[ ! -f "$CURRENT_FREEZE" || -L "$CURRENT_FREEZE" \
        || "$(stat -c %u:%h:%a "$CURRENT_FREEZE")" != "0:1:444" \
        || "$(stat -c %s "$CURRENT_FREEZE")" -gt 1048576 ]]; then
        echo "Current release freeze metadata is unsafe." >&2
        exit 1
      fi
      CURRENT_FREEZE_DIGEST="$(/usr/bin/sha256sum "$CURRENT_FREEZE")"
      CURRENT_FREEZE_DIGEST="${CURRENT_FREEZE_DIGEST%% *}"
      if [[ "$CURRENT_FREEZE_DIGEST" != "$EXPECTED_FREEZE_DIGEST" ]]; then
        echo "Current release freeze does not match rollback metadata." >&2
        exit 1
      fi
      LIVE_FREEZE_CHECK="$(mktemp "$SHARED_DIR/.rollback-freeze.XXXXXX")"
      if ! /usr/bin/env -i \
        HOME=/nonexistent \
        LANG=C.UTF-8 \
        LC_ALL=C.UTF-8 \
        PATH=/usr/bin:/bin \
        PIP_CONFIG_FILE=/dev/null \
        PIP_DISABLE_PIP_VERSION_CHECK=1 \
        PIP_NO_INDEX=1 \
        "$SHARED_VENV_DIR/bin/python" -I -m pip freeze --all \
        | /usr/bin/sort >"$LIVE_FREEZE_CHECK"; then
        rm -f -- "$LIVE_FREEZE_CHECK"
        echo "Shared venv freeze could not be verified for rollback." >&2
        exit 1
      fi
      chmod 0600 "$LIVE_FREEZE_CHECK"
      if ! /usr/bin/cmp -s "$CURRENT_FREEZE" "$LIVE_FREEZE_CHECK"; then
        rm -f -- "$LIVE_FREEZE_CHECK"
        echo "Shared venv changed after the skip-dependency install." >&2
        exit 1
      fi
      rm -f -- "$LIVE_FREEZE_CHECK"
      RESTORE_VENV=0
      VENV_RESTORE_MODE="unchanged-exact-freeze"
      ;;
    *)
      echo "Rollback venv transition record is invalid." >&2
      exit 1
      ;;
  esac
fi

cat <<EOF
Rollback plan
  app_dir:   $APP_DIR
  current:   $CURRENT_TARGET
  previous:  $PREVIOUS_TARGET
  restart:   $([[ "$RESTART_AFTER" -eq 1 ]] && echo yes || echo no)
  venv:      $VENV_RESTORE_MODE
  dry_run:   $([[ "$DRY_RUN" -eq 1 ]] && echo yes || echo no)

Warning:
  This switches release pointers and, unless explicitly skipped, the retained
  shared Python runtime. Database migrations are not automatically reversed.
EOF

if [[ "$DRY_RUN" -eq 1 ]]; then
  exit 0
fi

# A rollback changes current to the previous release. Install a stable,
# release-independent recovery bundle and a tiny handoff shim before that
# pointer switch, so a second process launched through the old current can
# still recover the new receipt and runtime phases.
prepare_recovery_bundle
install_recovery_shim "$PREVIOUS_TARGET"

ROLLBACK_COMPLETE=0
cleanup_failed_rollback() {
  if [[ "$ROLLBACK_COMPLETE" -eq 1 ]]; then
    return
  fi
  if [[ -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
    local pending_status pending_operation pending_phase
    pending_status="$(
      /usr/bin/python3 -I "$TRANSACTION_TOOL" status --state "$TRANSACTION_STATE" \
        2>/dev/null || true
    )"
    pending_operation="${pending_status%% *}"
    pending_phase="${pending_status#* }"
    if [[ "$pending_operation" == "rollback" && ( \
      "$pending_phase" == "rollback-runtime-pending" || \
      "$pending_phase" == "restart-pending" || \
      "$pending_phase" == "services-restarted" || \
      "$pending_phase" == "smoke-passed" || \
      "$pending_phase" == "rollback-runtime-applied" ) ]]; then
      if ! recover_rollback_to_original "$pending_phase"; then
        echo "CRITICAL: rollback runtime recovery failed; durable state was retained." >&2
      fi
    elif ! /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --state "$TRANSACTION_STATE"; then
      echo "CRITICAL: rollback recovery failed; durable state was retained." >&2
    fi
  fi
}
trap cleanup_failed_rollback EXIT

TRANSACTION_TRANSITION="none"
if [[ "$RESTORE_VENV" -eq 1 ]]; then
  TRANSACTION_TRANSITION="exchange"
fi
/usr/bin/python3 -I "$TRANSACTION_TOOL" create \
  --state "$TRANSACTION_STATE" \
  --operation rollback \
  --app-dir "$APP_DIR" \
  --current-before "$CURRENT_TARGET" \
  --previous-before "$PREVIOUS_TARGET" \
  --candidate-release "$CURRENT_TARGET" \
  --shared-venv "$SHARED_VENV_DIR" \
  --peer "$VENV_ROLLBACK_SNAPSHOT_DIR" \
  --snapshot "$VENV_ROLLBACK_SNAPSHOT_DIR" \
  --transition "$TRANSACTION_TRANSITION"

trap '' HUP INT TERM
if [[ "$RESTORE_VENV" -eq 1 ]]; then
  /usr/bin/python3 -I "$TRANSACTION_TOOL" exchange --state "$TRANSACTION_STATE"
fi
/usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
  --state "$TRANSACTION_STATE" \
  --expected prepared \
  --phase venv-transitioned
trap - HUP INT TERM

trap '' HUP INT TERM
/usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
  --state "$TRANSACTION_STATE" \
  --name current \
  --target "$PREVIOUS_TARGET"
/usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
  --state "$TRANSACTION_STATE" \
  --expected venv-transitioned \
  --phase current-switched
trap - HUP INT TERM

trap '' HUP INT TERM
/usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
  --state "$TRANSACTION_STATE" \
  --name previous \
  --target "$CURRENT_TARGET"
/usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
  --state "$TRANSACTION_STATE" \
  --expected current-switched \
  --phase pointers-switched
trap - HUP INT TERM

trap '' HUP INT TERM
/usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
  --state "$TRANSACTION_STATE" \
  --expected pointers-switched \
  --phase rollback-runtime-pending

"$RUNTIME_RESTORE_TOOL" \
  --app-dir "$APP_DIR" \
  --release "$PREVIOUS_TARGET" \
  --prepare-only

if [[ "$RESTART_AFTER" -eq 1 ]]; then
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected rollback-runtime-pending \
    --phase restart-pending
  "$RUNTIME_RESTORE_TOOL" \
    --app-dir "$APP_DIR" \
    --release "$PREVIOUS_TARGET" \
    --restart-only
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected restart-pending \
    --phase services-restarted
  "$RUNTIME_RESTORE_TOOL" \
    --app-dir "$APP_DIR" \
    --release "$PREVIOUS_TARGET" \
    --smoke-only
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected services-restarted \
    --phase smoke-passed
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected smoke-passed \
    --phase rollback-runtime-applied
else
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected rollback-runtime-pending \
    --phase rollback-runtime-applied
fi
/usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"
ROLLBACK_COMPLETE=1
trap - HUP INT TERM
trap - EXIT

cat <<EOF
Rollback complete.
  current:  $(readlink -f "$APP_DIR/current")
  previous: $(readlink -f "$APP_DIR/previous")
  venv:     $VENV_RESTORE_MODE
EOF
