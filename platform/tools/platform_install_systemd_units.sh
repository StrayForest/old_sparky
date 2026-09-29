#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_SRC_DIR="$ROOT_DIR/deploy/systemd"
SYSTEMD_DEST_DIR="${PLATFORM_SYSTEMD_DIR:-/etc/systemd/system}"
APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
ENABLE_SYSTEMD_UNITS="${PLATFORM_ENABLE_SYSTEMD_UNITS:-1}"
SYSTEMCTL_BIN="${PLATFORM_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"

# Every mutable systemctl call is bounded.  A wedged systemd manager must
# leave the caller's durable receipt available for retry rather than hanging
# a release indefinitely.  Tests may inject only the systemctl executable;
# the timeout implementation remains the trusted host path.
run_systemctl() {
  "$SYSTEMCTL_TIMEOUT_BIN" --signal=TERM --kill-after=5s 30s "$SYSTEMCTL_BIN" "$@"
}

# Do not treat an arbitrary systemctl failure (including timeout/rc=124) as an
# inactive or disabled unit.  Retired unit files remain in place until both
# state queries and any stop/disable operation have returned the canonical
# state/rc pair.
read_active_state() {
  local unit="$1" output status
  if output="$(run_systemctl is-active "$unit" 2>/dev/null)"; then
    status=0
  else
    status=$?
  fi
  case "$status:$output" in
    0:active) return 0 ;;
    3:inactive) return 3 ;;
    *)
      echo "Refusing retired-unit cleanup: invalid is-active result for $unit" >&2
      return 4
      ;;
  esac
}

read_enabled_state() {
  local unit="$1" output status
  if output="$(run_systemctl is-enabled "$unit" 2>/dev/null)"; then
    status=0
  else
    status=$?
  fi
  case "$status:$output" in
    0:enabled) return 0 ;;
    1:disabled) return 1 ;;
    *)
      echo "Refusing retired-unit cleanup: invalid is-enabled result for $unit" >&2
      return 4
      ;;
  esac
}

CURRENT_UNITS=(
  deadlock-api.service
  deadlock-worker.service
  deadlock-web.service
  deadlock-maintenance.service
  deadlock-maintenance.timer
  deadlock-logrotate.service
  deadlock-logrotate.timer
  deadlock-offsite-backup.service
  deadlock-offsite-backup.timer
  deadlock-cloudflare-ips.service
  deadlock-cloudflare-ips.timer
  deadlock-health-monitor.service
  deadlock-health-monitor.timer
)

declare -A EXPECTED_UNIT=()
for unit_name in "${CURRENT_UNITS[@]}"; do
  EXPECTED_UNIT["$unit_name"]=1
done

RETIRED_UNITS=()
RETIRED_UNIT_PATHS=()
declare -A RETIRED_ACTIVE_BEFORE=()
declare -A RETIRED_ENABLED_BEFORE=()
declare -A RETIRED_SOURCE_DIGEST=()
declare -A RETIRED_SOURCE_METADATA=()
declare -A RETIRED_BACKUP_PATH=()
RETIRED_BACKUP_DIR=""
RETIRED_BACKUP_IDENTITY=""
RETIRED_PHASE_INTENDED="active"
RETIRED_DURABLE_PHASE="active"
RETIRED_PHASE_WRITE_FAILED=0
RETIRED_CLEANUP_COMMITTED=0
RETIRED_TRANSACTION_ACTIVE=0
RETIRED_ROLLBACK_STATUS_PATH="$SYSTEMD_DEST_DIR/.oldsparky-retired-rollback-status"

retired_path_metadata() {
  stat -c '%F:%u:%g:%h:%a' -- "$1" 2>/dev/null
}

retired_file_digest() {
  local digest_line
  digest_line="$(/usr/bin/sha256sum -- "$1")" || return 1
  [[ "$digest_line" =~ ^([[:xdigit:]]{64})[[:space:]] ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}

retired_sync_path() {
  # fsync the opened path and its parent before the atomic rename.  The
  # additional coreutils sync calls cover filesystems where directory fsync is
  # unusually delayed; a SIGKILL must not leave a successfully reported but
  # non-durable transaction.
  /usr/bin/python3 - "$1" <<'PY'
import os
import sys

path = sys.argv[1]
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(path, flags)
try:
    os.fsync(fd)
finally:
    os.close(fd)
parent = os.path.dirname(os.path.abspath(path))
directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
directory_fd = os.open(parent, directory_flags)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  /usr/bin/sync -d -- "$1"
  /usr/bin/sync -f -- "$SYSTEMD_DEST_DIR"
}

retired_validate_record_path() {
  [[ ! -L "$RETIRED_ROLLBACK_STATUS_PATH" ]] || return 1
  if [[ -e "$RETIRED_ROLLBACK_STATUS_PATH" ]]; then
    [[ "$(retired_path_metadata "$RETIRED_ROLLBACK_STATUS_PATH")" == "regular file:0:0:1:600" ]] || return 1
  fi
}

retired_validate_unit_file() {
  local path="$1"
  [[ ! -L "$path" ]] || return 1
  [[ "$(retired_path_metadata "$path")" == "regular file:0:0:1:644" ]]
}

retired_validate_backup_dir() {
  local backup_dir="$1" canonical_dest canonical_backup
  [[ "$backup_dir" == "$SYSTEMD_DEST_DIR"/.oldsparky-retired.* ]] || return 1
  [[ -d "$backup_dir" && ! -L "$backup_dir" ]] || return 1
  [[ "$(retired_path_metadata "$backup_dir")" == "directory:0:0:2:700" ]] || return 1
  canonical_dest="$(readlink -f -- "$SYSTEMD_DEST_DIR")" || return 1
  canonical_backup="$(readlink -f -- "$backup_dir")" || return 1
  [[ "$canonical_backup" == "$canonical_dest"/.oldsparky-retired.* ]]
}

retired_validate_backup_location() {
  local backup_dir="$1" canonical_dest canonical_backup
  [[ "$backup_dir" == "$SYSTEMD_DEST_DIR"/.oldsparky-retired.* ]] || return 1
  canonical_dest="$(readlink -f -- "$SYSTEMD_DEST_DIR")" || return 1
  if [[ -e "$backup_dir" || -L "$backup_dir" ]]; then
    retired_validate_backup_dir "$backup_dir" || return 1
    canonical_backup="$(readlink -f -- "$backup_dir")" || return 1
    [[ "$canonical_backup" == "$canonical_dest"/.oldsparky-retired.* ]]
  else
    [[ ! -L "$backup_dir" ]]
  fi
}

retired_validate_restore_temp_dir() {
  local restore_dir="$1" canonical_dest canonical_restore
  [[ "$restore_dir" == "$SYSTEMD_DEST_DIR"/.oldsparky-restore.* ]] || return 1
  [[ -d "$restore_dir" && ! -L "$restore_dir" ]] || return 1
  [[ "$(retired_path_metadata "$restore_dir")" == "directory:0:0:2:700" ]] || return 1
  canonical_dest="$(readlink -f -- "$SYSTEMD_DEST_DIR")" || return 1
  canonical_restore="$(readlink -f -- "$restore_dir")" || return 1
  [[ "$canonical_restore" == "$canonical_dest"/.oldsparky-restore.* ]]
}

retired_test_pause() {
  local point="$1"
  [[ "${PLATFORM_TEST_RETIRED_PAUSE_POINT:-}" == "$point" ]] || return 0
  local marker="${PLATFORM_TEST_RETIRED_PAUSE_MARKER:-}"
  [[ "$marker" == /* ]] || return 1
  : > "$marker"
  /usr/bin/sleep 5
}

retired_test_fail_once() {
  local point="$1" marker="${PLATFORM_TEST_RETIRED_FAIL_MARKER:-}"
  [[ "${PLATFORM_TEST_RETIRED_FAIL_POINT:-}" == "$point" ]] || return 0
  # The phase-transition probes must not poison the initial active receipt.
  # They model a failure while publishing cleanup-pending, after the
  # transaction has already captured the exact pre-mutation state.
  if [[ "$point" == phase-status-write || "$point" == phase-status-fsync ]]; then
    [[ "$RETIRED_PHASE_INTENDED" == cleanup-pending ]] || return 0
  fi
  [[ "$marker" == /* ]] || return 1
  [[ ! -e "$marker" ]] || return 0
  : > "$marker"
  return 1
}

retired_reconcile_status_temps() {
  local path canonical_dest canonical_path
  local -a temporary_paths=()
  retired_validate_record_path || return 1
  canonical_dest="$(readlink -f -- "$SYSTEMD_DEST_DIR")" || return 1
  shopt -s nullglob
  temporary_paths=("$SYSTEMD_DEST_DIR"/.oldsparky-retired-status.*)
  shopt -u nullglob
  for path in "${temporary_paths[@]}"; do
    [[ ! -L "$path" ]] || {
      return 1
    }
    [[ "$(retired_path_metadata "$path")" == "regular file:0:0:1:600" ]] || {
      return 1
    }
    canonical_path="$(readlink -f -- "$path")" || {
      return 1
    }
    [[ "$canonical_path" == "$canonical_dest"/.oldsparky-retired-status.* ]] || {
      return 1
    }
  done
  if [[ ! -e "$RETIRED_ROLLBACK_STATUS_PATH" && "${#temporary_paths[@]}" -gt 0 ]]; then
    [[ "${#temporary_paths[@]}" -eq 1 ]] || return 1
    mv -f -- "${temporary_paths[0]}" "$RETIRED_ROLLBACK_STATUS_PATH" || return 1
    retired_sync_path "$RETIRED_ROLLBACK_STATUS_PATH"
    return $?
  fi
  for path in "${temporary_paths[@]}"; do
    rm -f -- "$path" || {
      return 1
    }
    retired_test_pause status-temp-unlink || {
      return 1
    }
  done
  /usr/bin/sync -f -- "$SYSTEMD_DEST_DIR"
}

retired_reject_orphans() {
  local path
  retired_reconcile_status_temps || return 1
  shopt -s nullglob
  if [[ ! -e "$RETIRED_ROLLBACK_STATUS_PATH" && ! -L "$RETIRED_ROLLBACK_STATUS_PATH" ]]; then
    for path in "$SYSTEMD_DEST_DIR"/.oldsparky-retired.*; do
      echo "Refusing retired-unit cleanup: orphan rollback backup exists: $path" >&2
      shopt -u nullglob
      return 1
    done
  fi
  for path in "$SYSTEMD_DEST_DIR"/.oldsparky-restore.*; do
    if [[ ! -e "$RETIRED_ROLLBACK_STATUS_PATH" && ! -L "$RETIRED_ROLLBACK_STATUS_PATH" ]]; then
      echo "Refusing retired-unit cleanup: orphan rollback temporary exists: $path" >&2
      shopt -u nullglob
      return 1
    fi
    retired_validate_restore_temp_dir "$path" || {
      echo "Refusing retired-unit cleanup: unsafe rollback temporary exists: $path" >&2
      shopt -u nullglob
      return 1
    }
  done
  shopt -u nullglob
}

write_retired_rollback_status() {
  local temporary_path backup_identity unit_name
  retired_validate_record_path || {
    echo "Cannot persist retired-unit rollback status: unsafe status path" >&2
    return 1
  }
  [[ -n "$RETIRED_BACKUP_DIR" ]] || return 1
  retired_validate_backup_dir "$RETIRED_BACKUP_DIR" || return 1
  backup_identity="$(retired_path_metadata "$RETIRED_BACKUP_DIR")" || return 1
  temporary_path="$(mktemp --tmpdir="$SYSTEMD_DEST_DIR" .oldsparky-retired-status.XXXXXX)" || return 1
  chmod 0600 -- "$temporary_path" || { rm -f -- "$temporary_path"; return 1; }
  if ! {
    printf 'schema=2\n'
    printf 'phase=%s\n' "$RETIRED_PHASE_INTENDED"
    printf 'backup_dir=%s\n' "$RETIRED_BACKUP_DIR"
    printf 'backup_identity=%s\n' "$backup_identity"
    for unit_name in "${RETIRED_UNITS[@]}"; do
      printf 'unit\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$unit_name" \
        "$SYSTEMD_DEST_DIR/$unit_name" \
        "${RETIRED_BACKUP_PATH[$unit_name]}" \
        "${RETIRED_SOURCE_DIGEST[$unit_name]}" \
        "${RETIRED_ACTIVE_BEFORE[$unit_name]}" \
        "${RETIRED_ENABLED_BEFORE[$unit_name]}" \
        "${RETIRED_SOURCE_METADATA[$unit_name]}"
    done
  } > "$temporary_path"; then
    rm -f -- "$temporary_path"
    return 1
  fi
  retired_sync_path "$temporary_path" || { rm -f -- "$temporary_path"; return 1; }
  retired_test_pause status-temp-write || { rm -f -- "$temporary_path"; return 1; }
  retired_test_fail_once phase-status-write || { rm -f -- "$temporary_path"; return 1; }
  mv -f -- "$temporary_path" "$RETIRED_ROLLBACK_STATUS_PATH" || {
    rm -f -- "$temporary_path"
    return 1
  }
  retired_test_pause status-temp-rename || return 1
  retired_test_fail_once phase-status-fsync || return 1
  retired_sync_path "$RETIRED_ROLLBACK_STATUS_PATH" || return 1
  RETIRED_DURABLE_PHASE="$RETIRED_PHASE_INTENDED"
}

clear_retired_rollback_status() {
  retired_validate_record_path || {
    echo "Refusing to clear unsafe retired-unit rollback status path" >&2
    return 1
  }
  if [[ -e "$RETIRED_ROLLBACK_STATUS_PATH" ]]; then
    [[ "$(retired_path_metadata "$RETIRED_ROLLBACK_STATUS_PATH")" == "regular file:0:0:1:600" ]] || {
      echo "Refusing to clear unsafe retired-unit rollback status path" >&2
      return 1
    }
    rm -f -- "$RETIRED_ROLLBACK_STATUS_PATH"
    /usr/bin/sync -f -- "$SYSTEMD_DEST_DIR"
  fi
}

restore_retired_unit_states() {
  local unit_name active_before enabled_before active_status enabled_status rollback_status=0
  for unit_name in "${RETIRED_UNITS[@]}"; do
    active_before="${RETIRED_ACTIVE_BEFORE[$unit_name]:-}"
    enabled_before="${RETIRED_ENABLED_BEFORE[$unit_name]:-}"
    if [[ "$active_before" == "active" ]]; then
      run_systemctl start "$unit_name" >/dev/null 2>&1 || rollback_status=1
    elif [[ "$active_before" == "inactive" ]]; then
      run_systemctl stop "$unit_name" >/dev/null 2>&1 || rollback_status=1
    fi
    if read_active_state "$unit_name" >/dev/null 2>&1; then
      active_status=0
    else
      active_status=$?
    fi
    if [[ "$active_before" == "active" && "$active_status" -ne 0 ]]; then
      echo "Retired-unit rollback could not restore active state: $unit_name" >&2
      rollback_status=1
    elif [[ "$active_before" == "inactive" && "$active_status" -ne 3 ]]; then
      echo "Retired-unit rollback could not restore inactive state: $unit_name" >&2
      rollback_status=1
    fi
    if [[ "$enabled_before" == "enabled" ]]; then
      run_systemctl enable "$unit_name" >/dev/null 2>&1 || rollback_status=1
    elif [[ "$enabled_before" == "disabled" ]]; then
      run_systemctl disable "$unit_name" >/dev/null 2>&1 || rollback_status=1
    fi
    if read_enabled_state "$unit_name" >/dev/null 2>&1; then
      enabled_status=0
    else
      enabled_status=$?
    fi
    if [[ "$enabled_before" == "enabled" && "$enabled_status" -ne 0 ]]; then
      echo "Retired-unit rollback could not restore enabled state: $unit_name" >&2
      rollback_status=1
    elif [[ "$enabled_before" == "disabled" && "$enabled_status" -ne 1 ]]; then
      echo "Retired-unit rollback could not restore disabled state: $unit_name" >&2
      rollback_status=1
    fi
  done
  return "$rollback_status"
}

retired_discard_uncommitted_backup() {
  local unit_name backup_path
  [[ ! -e "$RETIRED_ROLLBACK_STATUS_PATH" && ! -L "$RETIRED_ROLLBACK_STATUS_PATH" ]] || return 1
  [[ -n "$RETIRED_BACKUP_DIR" ]] || return 0
  retired_validate_backup_dir "$RETIRED_BACKUP_DIR" || return 1
  shopt -s nullglob
  for backup_path in "$RETIRED_BACKUP_DIR"/*; do
    unit_name="$(basename "$backup_path")"
    [[ -n "${RETIRED_BACKUP_PATH[$unit_name]+present}" ]] || {
      shopt -u nullglob
      return 1
    }
    retired_validate_unit_file "$backup_path" || {
      shopt -u nullglob
      return 1
    }
    rm -f -- "$backup_path" || {
      shopt -u nullglob
      return 1
    }
  done
  shopt -u nullglob
  rmdir -- "$RETIRED_BACKUP_DIR" || return 1
  /usr/bin/sync -f -- "$SYSTEMD_DEST_DIR"
  RETIRED_BACKUP_DIR=""
  RETIRED_BACKUP_IDENTITY=""
}

prepare_retired_transaction() {
  local unit_name unit_path backup_path prepare_status=0
  (( ${#RETIRED_UNITS[@]} > 0 )) || return 0
  [[ ! -e "$RETIRED_ROLLBACK_STATUS_PATH" && ! -L "$RETIRED_ROLLBACK_STATUS_PATH" ]] || return 1
  RETIRED_BACKUP_DIR="$(mktemp -d --tmpdir="$SYSTEMD_DEST_DIR" .oldsparky-retired.XXXXXX)" || return 1
  chmod 0700 -- "$RETIRED_BACKUP_DIR" || prepare_status=1
  retired_validate_backup_dir "$RETIRED_BACKUP_DIR" || prepare_status=1
  RETIRED_BACKUP_IDENTITY="$(retired_path_metadata "$RETIRED_BACKUP_DIR")" || prepare_status=1
  for unit_name in "${RETIRED_UNITS[@]}"; do
    unit_path="$SYSTEMD_DEST_DIR/$unit_name"
    backup_path="$RETIRED_BACKUP_DIR/$unit_name"
    RETIRED_BACKUP_PATH["$unit_name"]="$backup_path"
    retired_validate_unit_file "$unit_path" || prepare_status=1
    [[ "$(retired_file_digest "$unit_path")" == "${RETIRED_SOURCE_DIGEST[$unit_name]}" ]] || prepare_status=1
    install -o root -g root -m 0644 -- "$unit_path" "$backup_path" || prepare_status=1
    retired_validate_unit_file "$backup_path" || prepare_status=1
    [[ "$(retired_file_digest "$backup_path")" == "${RETIRED_SOURCE_DIGEST[$unit_name]}" ]] || prepare_status=1
  done
  retired_sync_path "$RETIRED_BACKUP_DIR" || prepare_status=1
  write_retired_rollback_status || prepare_status=1
  if [[ "$prepare_status" -ne 0 ]]; then
    echo "Refusing retired-unit cleanup: rollback backup validation failed" >&2
    retired_discard_uncommitted_backup || true
    return 1
  fi
  RETIRED_TRANSACTION_ACTIVE=1
}

retired_validate_backup_entries() {
  local unit_name backup_path expected_digest actual_digest
  retired_validate_backup_dir "$RETIRED_BACKUP_DIR" || return 1
  [[ "$(retired_path_metadata "$RETIRED_BACKUP_DIR")" == "$RETIRED_BACKUP_IDENTITY" ]] || return 1
  shopt -s nullglob
  for backup_path in "$RETIRED_BACKUP_DIR"/*; do
    unit_name="$(basename "$backup_path")"
    [[ -n "${RETIRED_BACKUP_PATH[$unit_name]+present}" ]] || {
      shopt -u nullglob
      return 1
    }
    [[ "$backup_path" == "${RETIRED_BACKUP_PATH[$unit_name]}" ]] || {
      shopt -u nullglob
      return 1
    }
    retired_validate_unit_file "$backup_path" || {
      shopt -u nullglob
      return 1
    }
    expected_digest="${RETIRED_SOURCE_DIGEST[$unit_name]}"
    actual_digest="$(retired_file_digest "$backup_path")" || {
      shopt -u nullglob
      return 1
    }
    [[ "$actual_digest" == "$expected_digest" ]] || {
      shopt -u nullglob
      return 1
    }
  done
  shopt -u nullglob
  for unit_name in "${RETIRED_UNITS[@]}"; do
    [[ -f "${RETIRED_BACKUP_PATH[$unit_name]}" ]] || return 1
  done
}

retired_restore_unit_files() {
  local unit_name backup_path target_path temporary_dir temporary_path
  retired_validate_backup_entries || {
    echo "Refusing retired-unit rollback: backup identity or digest is invalid" >&2
    return 1
  }
  temporary_dir="$(mktemp -d --tmpdir="$SYSTEMD_DEST_DIR" .oldsparky-restore.XXXXXX)" || return 1
  chmod 0700 -- "$temporary_dir" || return 1
  retired_path_metadata "$temporary_dir" | grep -qx 'directory:0:0:2:700' || return 1
  for unit_name in "${RETIRED_UNITS[@]}"; do
    backup_path="${RETIRED_BACKUP_PATH[$unit_name]}"
    target_path="$SYSTEMD_DEST_DIR/$unit_name"
    if [[ -e "$target_path" || -L "$target_path" ]]; then
      retired_validate_unit_file "$target_path" || return 1
      [[ "$(retired_file_digest "$target_path")" == "${RETIRED_SOURCE_DIGEST[$unit_name]}" ]] || return 1
    fi
    temporary_path="$temporary_dir/$unit_name"
    install -o root -g root -m 0644 -- "$backup_path" "$temporary_path" || return 1
    retired_validate_unit_file "$temporary_path" || return 1
    [[ "$(retired_file_digest "$temporary_path")" == "${RETIRED_SOURCE_DIGEST[$unit_name]}" ]] || return 1
    retired_sync_path "$temporary_path" || return 1
    retired_test_pause restore-temp-write || return 1
    mv -f -- "$temporary_path" "$target_path" || return 1
    retired_validate_unit_file "$target_path" || return 1
    [[ "$(retired_file_digest "$target_path")" == "${RETIRED_SOURCE_DIGEST[$unit_name]}" ]] || return 1
    retired_test_pause restore-temp-rename || return 1
  done
  rmdir -- "$temporary_dir" || return 1
  /usr/bin/sync -f -- "$SYSTEMD_DEST_DIR"
}

retired_cleanup_restore_temps() {
  local restore_dir restore_path unit_name
  shopt -s nullglob
  for restore_dir in "$SYSTEMD_DEST_DIR"/.oldsparky-restore.*; do
    retired_validate_restore_temp_dir "$restore_dir" || {
      shopt -u nullglob
      return 1
    }
    for restore_path in "$restore_dir"/*; do
      unit_name="$(basename "$restore_path")"
      [[ -n "${RETIRED_BACKUP_PATH[$unit_name]+present}" ]] || {
        shopt -u nullglob
        return 1
      }
      retired_validate_unit_file "$restore_path" || {
        shopt -u nullglob
        return 1
      }
      [[ "$(retired_file_digest "$restore_path")" == "${RETIRED_SOURCE_DIGEST[$unit_name]}" ]] || {
        shopt -u nullglob
        return 1
      }
      rm -f -- "$restore_path" || {
        shopt -u nullglob
        return 1
      }
      retired_test_pause restore-temp-unlink || {
        shopt -u nullglob
        return 1
      }
    done
    rmdir -- "$restore_dir" || {
      shopt -u nullglob
      return 1
    }
    retired_test_pause restore-temp-rmdir || {
      shopt -u nullglob
      return 1
    }
  done
  shopt -u nullglob
  /usr/bin/sync -f -- "$SYSTEMD_DEST_DIR"
}

retired_validate_remaining_backup_entries() {
  local unit_name backup_path expected_digest actual_digest
  retired_validate_backup_location "$RETIRED_BACKUP_DIR" || return 1
  if [[ ! -e "$RETIRED_BACKUP_DIR" ]]; then
    return 0
  fi
  [[ "$(retired_path_metadata "$RETIRED_BACKUP_DIR")" == "$RETIRED_BACKUP_IDENTITY" ]] || return 1
  shopt -s nullglob
  for backup_path in "$RETIRED_BACKUP_DIR"/*; do
    unit_name="$(basename "$backup_path")"
    [[ -n "${RETIRED_BACKUP_PATH[$unit_name]+present}" ]] || {
      shopt -u nullglob
      return 1
    }
    [[ "$backup_path" == "${RETIRED_BACKUP_PATH[$unit_name]}" ]] || {
      shopt -u nullglob
      return 1
    }
    retired_validate_unit_file "$backup_path" || {
      shopt -u nullglob
      return 1
    }
    expected_digest="${RETIRED_SOURCE_DIGEST[$unit_name]}"
    actual_digest="$(retired_file_digest "$backup_path")" || {
      shopt -u nullglob
      return 1
    }
    [[ "$actual_digest" == "$expected_digest" ]] || {
      shopt -u nullglob
      return 1
    }
  done
  shopt -u nullglob
}

retired_cleanup_pending() {
  local unit_name backup_path
  retired_validate_remaining_backup_entries || {
    echo "Refusing retired-unit cleanup: unsafe cleanup-pending backup" >&2
    return 1
  }
  if [[ -e "$RETIRED_BACKUP_DIR" ]]; then
    for unit_name in "${RETIRED_UNITS[@]}"; do
      backup_path="${RETIRED_BACKUP_PATH[$unit_name]}"
      if [[ -e "$backup_path" || -L "$backup_path" ]]; then
        retired_validate_unit_file "$backup_path" || return 1
        rm -f -- "$backup_path" || return 1
        retired_test_pause backup-unlink || return 1
      fi
    done
    rmdir -- "$RETIRED_BACKUP_DIR" || return 1
    retired_test_pause backup-rmdir || return 1
  fi
  /usr/bin/sync -f -- "$SYSTEMD_DEST_DIR" || return 1
  clear_retired_rollback_status || return 1
  retired_test_pause status-clear || return 1
  RETIRED_BACKUP_DIR=""
  RETIRED_BACKUP_IDENTITY=""
  RETIRED_PHASE_INTENDED="active"
  RETIRED_DURABLE_PHASE="active"
  RETIRED_TRANSACTION_ACTIVE=0
  RETIRED_CLEANUP_COMMITTED=1
}

retired_commit_transaction() {
  local previous_durable_phase="$RETIRED_DURABLE_PHASE"
  retired_validate_backup_entries || return 1
  RETIRED_PHASE_INTENDED="cleanup-pending"
  if ! write_retired_rollback_status; then
    # The phase is not authoritative until the replacement receipt has been
    # atomically renamed and fsynced.  Restore an active receipt when possible;
    # the EXIT path will restore files/states but must not delete the backup.
    RETIRED_PHASE_INTENDED="$previous_durable_phase"
    RETIRED_PHASE_WRITE_FAILED=1
    write_retired_rollback_status || true
    return 1
  fi
  retired_test_pause cleanup-phase-record || return 1
  retired_cleanup_pending
}

restore_retired_units() {
  local status=$? rollback_status=0
  trap - EXIT
  set +e
  if [[ "$RETIRED_TRANSACTION_ACTIVE" -eq 1 && "$RETIRED_CLEANUP_COMMITTED" -eq 0 ]]; then
    if [[ "$RETIRED_PHASE_WRITE_FAILED" -eq 1 ]]; then
      retired_restore_unit_files || rollback_status=1
      if [[ "$rollback_status" -eq 0 ]]; then
        run_systemctl daemon-reload >/dev/null 2>&1 || rollback_status=1
      fi
      [[ "$rollback_status" -eq 0 ]] && restore_retired_unit_states || rollback_status=1
    elif [[ "$RETIRED_DURABLE_PHASE" == "cleanup-pending" ]]; then
      retired_cleanup_pending || rollback_status=1
    else
      retired_restore_unit_files || rollback_status=1
      if [[ "$rollback_status" -eq 0 ]]; then
        run_systemctl daemon-reload >/dev/null 2>&1 || rollback_status=1
      fi
      [[ "$rollback_status" -eq 0 ]] && restore_retired_unit_states || rollback_status=1
      if [[ "$rollback_status" -eq 0 ]]; then
        retired_commit_transaction || rollback_status=1
      fi
    fi
  fi
  if [[ "$rollback_status" -ne 0 ]]; then
    echo "Retired-unit rollback failed; durable status and backup retained for retry" >&2
    status=1
  fi
  exit "$status"
}

resume_retired_rollback() {
  [[ -e "$RETIRED_ROLLBACK_STATUS_PATH" || -L "$RETIRED_ROLLBACK_STATUS_PATH" ]] || return 0
  retired_validate_record_path || {
    echo "Refusing retired-unit retry: rollback status metadata is unsafe" >&2
    return 1
  }
  local schema_seen=0 phase_seen=0 backup_dir_seen=0 backup_identity_seen=0
  local record_phase=""
  local kind unit_name unit_path backup_path digest active_before enabled_before source_metadata extra line
  local parse_status=0
  while IFS= read -r line; do
    case "$line" in
      schema=2)
        [[ "$schema_seen" -eq 0 ]] || parse_status=1
        schema_seen=1
        ;;
      phase=active)
        [[ "$phase_seen" -eq 0 ]] || parse_status=1
        phase_seen=1
        record_phase="active"
        ;;
      phase=cleanup-pending)
        [[ "$phase_seen" -eq 0 ]] || parse_status=1
        phase_seen=1
        record_phase="cleanup-pending"
        ;;
      phase=*)
        parse_status=1
        ;;
      backup_dir=*)
        [[ "$backup_dir_seen" -eq 0 ]] || parse_status=1
        backup_dir_seen=1
        RETIRED_BACKUP_DIR="${line#backup_dir=}"
        ;;
      backup_identity=*)
        [[ "$backup_identity_seen" -eq 0 ]] || parse_status=1
        backup_identity_seen=1
        RETIRED_BACKUP_IDENTITY="${line#backup_identity=}"
        ;;
      unit$'\t'*)
        IFS=$'\t' read -r kind unit_name unit_path backup_path digest active_before enabled_before source_metadata extra <<< "$line"
        [[ -z "${extra:-}" && "$kind" == unit ]] || parse_status=1
        [[ "$unit_name" =~ ^deadlock-[A-Za-z0-9_.-]+\.(service|timer)$ ]] || parse_status=1
        [[ "$unit_path" == "$SYSTEMD_DEST_DIR/$unit_name" ]] || parse_status=1
        [[ "$backup_path" == "$RETIRED_BACKUP_DIR/$unit_name" ]] || parse_status=1
        [[ "$digest" =~ ^[[:xdigit:]]{64}$ ]] || parse_status=1
        [[ "$active_before" == active || "$active_before" == inactive ]] || parse_status=1
        [[ "$enabled_before" == enabled || "$enabled_before" == disabled ]] || parse_status=1
        [[ "$source_metadata" == "regular file:0:0:1:644" ]] || parse_status=1
        [[ -z "${RETIRED_ACTIVE_BEFORE[$unit_name]+present}" ]] || parse_status=1
        RETIRED_UNITS+=("$unit_name")
        RETIRED_UNIT_PATHS+=("$unit_path")
        RETIRED_ACTIVE_BEFORE["$unit_name"]="$active_before"
        RETIRED_ENABLED_BEFORE["$unit_name"]="$enabled_before"
        RETIRED_SOURCE_DIGEST["$unit_name"]="$digest"
        RETIRED_SOURCE_METADATA["$unit_name"]="$source_metadata"
        RETIRED_BACKUP_PATH["$unit_name"]="$backup_path"
        ;;
      *)
        parse_status=1
        ;;
    esac
  done < "$RETIRED_ROLLBACK_STATUS_PATH"
  [[ "$schema_seen" -eq 1 && "$phase_seen" -eq 1 && "$backup_dir_seen" -eq 1 && "$backup_identity_seen" -eq 1 \
    && "$parse_status" -eq 0 && "${#RETIRED_UNITS[@]}" -gt 0 ]] || {
    echo "Refusing retired-unit retry: rollback status schema is invalid" >&2
    return 1
  }
  # Only a fully parsed, validated receipt can establish the in-memory phase.
  # In particular, never resume from an intended phase left by a prior process
  # before its atomic replacement receipt was fsynced.
  RETIRED_PHASE_INTENDED="$record_phase"
  RETIRED_DURABLE_PHASE="$record_phase"
  RETIRED_PHASE_WRITE_FAILED=0
  retired_validate_backup_location "$RETIRED_BACKUP_DIR" || return 1
  shopt -s nullglob
  local candidate_backup
  for candidate_backup in "$SYSTEMD_DEST_DIR"/.oldsparky-retired.*; do
    [[ "$candidate_backup" == "$RETIRED_BACKUP_DIR" ]] || {
      shopt -u nullglob
      echo "Refusing retired-unit retry: unexpected rollback backup exists" >&2
      return 1
    }
  done
  shopt -u nullglob
  retired_cleanup_restore_temps || return 1
  RETIRED_TRANSACTION_ACTIVE=1
  if [[ "$RETIRED_DURABLE_PHASE" == "cleanup-pending" ]]; then
    retired_cleanup_pending || return 1
    RETIRED_UNITS=()
    RETIRED_UNIT_PATHS=()
    RETIRED_ACTIVE_BEFORE=()
    RETIRED_ENABLED_BEFORE=()
    RETIRED_SOURCE_DIGEST=()
    RETIRED_SOURCE_METADATA=()
    RETIRED_BACKUP_PATH=()
    return 0
  fi
  retired_validate_backup_entries || return 1
  retired_restore_unit_files || return 1
  run_systemctl daemon-reload || return 1
  restore_retired_unit_states || return 1
  retired_commit_transaction || return 1
  RETIRED_UNITS=()
  RETIRED_UNIT_PATHS=()
  RETIRED_ACTIVE_BEFORE=()
  RETIRED_ENABLED_BEFORE=()
  RETIRED_SOURCE_DIGEST=()
  RETIRED_SOURCE_METADATA=()
  RETIRED_BACKUP_PATH=()
}

retired_reject_orphans || exit 1
resume_retired_rollback || exit 1
trap restore_retired_units EXIT

if [[ "$SYSTEMD_DEST_DIR" == "/etc/systemd/system" && "$ENABLE_SYSTEMD_UNITS" == "1" ]]; then
  shopt -s nullglob
  for unit_path in "$SYSTEMD_DEST_DIR"/deadlock-*.service "$SYSTEMD_DEST_DIR"/deadlock-*.timer; do
    unit_name="$(basename "$unit_path")"
    if [[ -n "${EXPECTED_UNIT[$unit_name]:-}" ]]; then
      continue
    fi
    [[ "$(stat -c '%F:%u:%g:%h:%a' -- "$unit_path" 2>/dev/null)" == "regular file:0:0:1:644" ]] || {
      echo "Refusing retired-unit cleanup: unit file metadata is unsafe for $unit_name" >&2
      exit 1
    }
    active_status=0
    if read_active_state "$unit_name"; then
      active_status=0
    else
      active_status=$?
    fi
    case "$active_status" in
      0) RETIRED_ACTIVE_BEFORE["$unit_name"]="active" ;;
      3) RETIRED_ACTIVE_BEFORE["$unit_name"]="inactive" ;;
      *)
        echo "Refusing retired-unit cleanup: cannot establish active state for $unit_name" >&2
        exit 1
        ;;
    esac
    enabled_status=0
    if read_enabled_state "$unit_name"; then
      enabled_status=0
    else
      enabled_status=$?
    fi
    case "$enabled_status" in
      0) RETIRED_ENABLED_BEFORE["$unit_name"]="enabled" ;;
      1) RETIRED_ENABLED_BEFORE["$unit_name"]="disabled" ;;
      *)
        echo "Refusing retired-unit cleanup: cannot establish enabled state for $unit_name" >&2
        exit 1
        ;;
    esac
    # Capture both exact states before the first stop/disable mutation so the
    # durable transaction can restore them even when the later install fails.
    RETIRED_UNIT_PATHS+=("$unit_path")
    RETIRED_UNITS+=("$unit_name")
    RETIRED_SOURCE_METADATA["$unit_name"]="$(retired_path_metadata "$unit_path")"
    RETIRED_SOURCE_DIGEST["$unit_name"]="$(retired_file_digest "$unit_path")"
    [[ -n "${RETIRED_SOURCE_DIGEST[$unit_name]}" ]] || {
      echo "Refusing retired-unit cleanup: cannot digest $unit_name" >&2
      exit 1
    }
  done
  shopt -u nullglob
fi

# The record and exact file backups are durable before the first stop/disable
# mutation.  A SIGKILL at any later point therefore leaves enough information
# for the next invocation to restore the old files and states without adopting
# an unknown directory.
prepare_retired_transaction || {
  echo "Refusing retired-unit cleanup: could not persist rollback transaction" >&2
  exit 1
}

for unit_name in "${RETIRED_UNITS[@]}"; do
  if [[ "${RETIRED_ACTIVE_BEFORE[$unit_name]}" == "active" ]]; then
    run_systemctl stop "$unit_name"
    post_stop_status=0
    if read_active_state "$unit_name"; then
      post_stop_status=0
    else
      post_stop_status=$?
    fi
    [[ "$post_stop_status" -eq 3 ]] || {
      echo "Refusing retired-unit cleanup: $unit_name remained active" >&2
      exit 1
    }
  fi
  if [[ "${RETIRED_ENABLED_BEFORE[$unit_name]}" == "enabled" ]]; then
    run_systemctl disable "$unit_name"
    post_disable_status=0
    if read_enabled_state "$unit_name"; then
      post_disable_status=0
    else
      post_disable_status=$?
    fi
    [[ "$post_disable_status" -eq 1 ]] || {
      echo "Refusing retired-unit cleanup: $unit_name remained enabled" >&2
      exit 1
    }
  fi
done

for unit_name in "${CURRENT_UNITS[@]}"; do
  install -o root -g root -m 0644 "$SYSTEMD_SRC_DIR/$unit_name" "$SYSTEMD_DEST_DIR/$unit_name"
done

if [[ "$SYSTEMD_DEST_DIR" == "/etc/systemd/system" ]]; then
  "$ROOT_DIR/tools/platform_install_logging.sh"
fi

"$ROOT_DIR/tools/platform_prepare_service_user.sh" \
  --app-dir "$APP_DIR" \
  --apply
run_systemctl daemon-reload

if [[ "$ENABLE_SYSTEMD_UNITS" == "1" ]]; then
  run_systemctl enable deadlock-api.service deadlock-worker.service deadlock-web.service
  run_systemctl enable --now \
    deadlock-maintenance.timer \
    deadlock-logrotate.timer \
    deadlock-cloudflare-ips.timer \
    deadlock-health-monitor.timer
fi

# Keep retired files in place until the new unit set has been installed and
# systemd has accepted a daemon-reload.  The durable pre-mutation transaction
# is committed only after the final removal and daemon-reload succeed.
if (( ${#RETIRED_UNIT_PATHS[@]} > 0 )); then
  for unit_path in "${RETIRED_UNIT_PATHS[@]}"; do
    unit_name="$(basename "$unit_path")"
    retired_validate_unit_file "$unit_path" || {
      echo "Refusing retired-unit cleanup: retired file changed before removal" >&2
      exit 1
    }
    [[ "$(retired_file_digest "$unit_path")" == "${RETIRED_SOURCE_DIGEST[$unit_name]}" ]] || {
      echo "Refusing retired-unit cleanup: retired file digest changed before removal" >&2
      exit 1
    }
    rm -f -- "$unit_path"
  done
  run_systemctl daemon-reload
  retired_commit_transaction || {
    echo "Refusing retired-unit cleanup: rollback transaction could not be committed" >&2
    exit 1
  }
fi

cat <<EOF
Installed platform systemd units into:
  $SYSTEMD_DEST_DIR

Units:
$(printf '  %s\n' "${CURRENT_UNITS[@]}")
Prepared service-owned runtime paths under:
  $APP_DIR
EOF

if (( ${#RETIRED_UNITS[@]} > 0 )); then
  printf 'Removed retired managed units:\n'
  printf '  %s\n' "${RETIRED_UNITS[@]}"
fi

# The public SSH host-key fingerprint is intentionally emitted during real
# production activation so CI can pin the already-trusted server in the next
# release. This never reads or exposes the private host key.
if [[ "$SYSTEMD_DEST_DIR" == "/etc/systemd/system" \
  && -x /usr/bin/ssh-keygen \
  && -f /etc/ssh/ssh_host_ed25519_key.pub \
  && ! -L /etc/ssh/ssh_host_ed25519_key.pub ]]; then
  host_key_meta="$(stat -c '%u:%g:%a:%h' /etc/ssh/ssh_host_ed25519_key.pub)"
  if [[ "$host_key_meta" == "0:0:644:1" || "$host_key_meta" == "0:0:640:1" ]]; then
    host_key_fingerprint="$(
      /usr/bin/ssh-keygen -E sha256 -lf /etc/ssh/ssh_host_ed25519_key.pub \
        | /usr/bin/awk '{print $2}'
    )"
    if [[ "$host_key_fingerprint" == SHA256:* ]]; then
      printf 'PRODUCTION_SSH_ED25519_FINGERPRINT=%s\n' "$host_key_fingerprint"
    fi
  fi
fi
