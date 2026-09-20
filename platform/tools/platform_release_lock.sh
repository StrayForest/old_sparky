#!/usr/bin/env bash

# Canonical release lock shared by the release state machine, deploy
# supervisors and recovery entrypoints.  This file is sourced by those
# entrypoints and deliberately does not set shell options or acquire a lock
# merely by being sourced.
#
# There is intentionally no environment-controlled test pathname here.  A
# production entrypoint must not be able to replace the canonical lock by
# setting PLATFORM_ENVIRONMENT/PLATFORM_TESTING (or any other ambient value).
# Privileged tests use a serial guard and unique root-owned files directly
# beneath /run/lock; the production canonical pathname remains unchanged.

PLATFORM_RELEASE_LOCK_CANONICAL_PATH="/run/lock/oldsparky-platform-release.lock"
PLATFORM_RELEASE_LOCK_PATH="$PLATFORM_RELEASE_LOCK_CANONICAL_PATH"
PLATFORM_RETAINED_LOAD_LOCK_CANONICAL_PATH="/run/lock/oldsparky-retained-load-matrix.lock"
PLATFORM_RETAINED_LOAD_LOCK_PATH="$PLATFORM_RETAINED_LOAD_LOCK_CANONICAL_PATH"

# Keep lock acquisition failures distinct from the release body's exit status.
# 73 is not used by any release entrypoint; callers translate it to their
# public lock-contention status (3).
PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE=73

platform_release_lock_select_path() {
  # A stale test-only variable is rejected rather than interpreted.  This is
  # an explicit regression guard against production app-dir lock bypasses.
  [[ -z "${PLATFORM_TEST_RELEASE_LOCK_PATH:-}" ]] || return 1
  PLATFORM_RELEASE_LOCK_PATH="$PLATFORM_RELEASE_LOCK_CANONICAL_PATH"
}

platform_release_lock_validate_root() {
  local lock_root="$1" root_real root_uid root_mode
  [[ "$lock_root" == "/run/lock" ]] || return 1
  [[ -d "$lock_root" && ! -L "$lock_root" ]] || return 1
  root_real="$(/usr/bin/readlink -f -- "$lock_root" 2>/dev/null)" || return 1
  [[ "$root_real" == "$lock_root" ]] || return 1
  root_uid="$(/usr/bin/stat -c %u -- "$lock_root" 2>/dev/null)" || return 1
  root_mode="$(/usr/bin/stat -c %a -- "$lock_root" 2>/dev/null)" || return 1
  [[ "$root_uid" == "0" && "$root_mode" =~ ^[0-7]+$ ]] || return 1
  # A root-owned sticky directory is safe for the fixed lock name.  A
  # group/world-writable non-sticky root is not: another account could swap
  # the pathname between validation and open.
  if (( (8#$root_mode & 8#022) != 0 )) \
    && (( (8#$root_mode & 8#1000) == 0 )); then
    return 1
  fi
}

platform_release_lock_validate_file_metadata() {
  local lock_path="$1" lock_uid lock_gid lock_links lock_mode
  [[ -f "$lock_path" && ! -L "$lock_path" ]] || return 1
  lock_uid="$(/usr/bin/stat -c %u -- "$lock_path" 2>/dev/null)" || return 1
  lock_gid="$(/usr/bin/stat -c %g -- "$lock_path" 2>/dev/null)" || return 1
  lock_links="$(/usr/bin/stat -c %h -- "$lock_path" 2>/dev/null)" || return 1
  lock_mode="$(/usr/bin/stat -c %a -- "$lock_path" 2>/dev/null)" || return 1
  [[ "$lock_uid:$lock_gid:$lock_links:$lock_mode" == "0:0:1:600" ]]
}

platform_release_lock_create_and_validate() {
  local lock_path="$1" lock_root
  lock_root="$(/usr/bin/dirname -- "$lock_path")" || return 1
  platform_release_lock_validate_root "$lock_root" || return 1

  # NOCLOBBER prevents this process from replacing an existing path.  The
  # post-open device/inode check below closes the remaining path-swap race.
  if [[ -L "$lock_path" ]]; then
    return 1
  fi
  if [[ ! -e "$lock_path" ]]; then
    if ! (
      umask 077
      set -o noclobber
      : >"$lock_path"
    ) 2>/dev/null; then
      [[ -e "$lock_path" && ! -L "$lock_path" ]] || return 1
    else
      /usr/bin/chown 0:0 -- "$lock_path" || return 1
      /usr/bin/chmod 0600 -- "$lock_path" || return 1
    fi
  fi
  platform_release_lock_validate_file_metadata "$lock_path"
}

platform_release_lock_prepare() {
  platform_release_lock_select_path || return 1
  platform_release_lock_create_and_validate "$PLATFORM_RELEASE_LOCK_PATH"
}

platform_release_lock_path_identity() {
  /usr/bin/stat -c '%d:%i' -- "$PLATFORM_RELEASE_LOCK_PATH" 2>/dev/null
}

platform_release_lock_fd_identity() {
  local descriptor="$1"
  /usr/bin/stat -Lc '%d:%i' -- "/proc/self/fd/$descriptor" 2>/dev/null
}

platform_release_lock_fd_matches_path() {
  local descriptor="$1" lock_path="$2" path_identity descriptor_identity
  [[ "$descriptor" =~ ^[0-9]+$ ]] || return 1
  [[ -e "/proc/self/fd/$descriptor" ]] || return 1
  path_identity="$(/usr/bin/stat -c '%d:%i' -- "$lock_path" 2>/dev/null)" || return 1
  descriptor_identity="$(platform_release_lock_fd_identity "$descriptor")" || return 1
  [[ "$path_identity" == "$descriptor_identity" ]]
}

platform_release_lock_matches_path() {
  platform_release_lock_fd_matches_path "$1" "$PLATFORM_RELEASE_LOCK_PATH"
}

platform_lock_expected_device() {
  local lock_path="$1" raw device_number major minor
  raw="$(/usr/bin/stat -c %D -- "$lock_path" 2>/dev/null)" || return 1
  [[ "$raw" =~ ^[0-9a-fA-F]+$ ]] || return 1
  device_number=$((16#$raw))
  major=$(((device_number >> 8) & 0xfff))
  minor=$(((device_number & 0xff) | ((device_number >> 12) & 0xfff00)))
  printf '%x:%x\n' "$major" "$minor"
}

platform_release_lock_expected_device() {
  platform_lock_expected_device "$PLATFORM_RELEASE_LOCK_PATH"
}

platform_lock_validate_supervisor_pid_for_path() {
  local supervisor_pid="$1" lock_path="$2" supervisor_exe
  local descriptor_path descriptor target
  [[ "$supervisor_pid" =~ ^[0-9]+$ && "$supervisor_pid" -gt 1 ]] || return 1
  [[ -r "/proc/$supervisor_pid/exe" && -d "/proc/$supervisor_pid/fd" ]] || return 1
  supervisor_exe="$(/usr/bin/readlink -f -- "/proc/$supervisor_pid/exe" 2>/dev/null)"
  [[ "$supervisor_exe" == \
    "/usr/bin/flock" ]] || return 1

  local lock_device lock_inode expected_device
  lock_device="$(platform_lock_expected_device "$lock_path")" || return 1
  expected_device="$lock_device"
  lock_inode="$(/usr/bin/stat -c %i -- "$lock_path" 2>/dev/null)" || return 1
  [[ "$lock_inode" =~ ^[0-9]+$ ]] || return 1

  # /proc/locks uses: ID: FLOCK ADVISORY WRITE PID MAJOR:MINOR:INODE ... .
  # Require all four ownership facts.  In particular, SHARED/READ locks and a
  # record owned by a different PID must never validate a release supervisor.
  /usr/bin/awk \
    -v expected_device="$expected_device" \
    -v expected_inode="$lock_inode" \
    -v expected_pid="$supervisor_pid" \
    'function normalized(value) {
       value = tolower(value)
       sub(/^0+/, "", value)
       return value == "" ? "0" : value
     }
     $2 == "FLOCK" && $4 == "WRITE" && $5 == expected_pid {
       split($6, device, ":")
       current = normalized(device[1]) ":" normalized(device[2])
       if (current == tolower(expected_device) && device[3] == expected_inode) {
         found = 1
       }
     }
     END { exit(found ? 0 : 1) }' \
    /proc/locks 2>/dev/null || return 1

  for descriptor_path in "/proc/$supervisor_pid"/fd/*; do
    [[ -e "$descriptor_path" ]] || continue
    target="$(/usr/bin/readlink -- "$descriptor_path" 2>/dev/null || true)"
    [[ "$target" == "$lock_path" ]] || continue
    descriptor="${descriptor_path##*/}"
    [[ "$descriptor" =~ ^[0-9]+$ ]] || continue
    if [[ "$(/usr/bin/stat -Lc '%d:%i' -- "$descriptor_path" 2>/dev/null)" == \
      "$(/usr/bin/stat -c '%d:%i' -- "$lock_path" 2>/dev/null)" ]]; then
      return 0
    fi
  done
  return 1
}

platform_release_lock_validate_supervisor_pid() {
  platform_lock_validate_supervisor_pid_for_path "$1" "$PLATFORM_RELEASE_LOCK_PATH"
}

platform_retained_load_lock_validate_supervisor_pid() {
  platform_lock_validate_supervisor_pid_for_path "$1" "$PLATFORM_RETAINED_LOAD_LOCK_PATH"
}

platform_release_lock_supervisor_holds() {
  platform_release_lock_select_path || return 1
  platform_release_lock_prepare || return 1
  local process_pid="$$" parent_pid
  while [[ "$process_pid" =~ ^[0-9]+$ && "$process_pid" -gt 1 ]]; do
    if platform_release_lock_validate_supervisor_pid "$process_pid"; then
      return 0
    fi
    parent_pid="$(/usr/bin/awk '{print $4}' "/proc/$process_pid/stat" 2>/dev/null || true)"
    [[ "$parent_pid" =~ ^[0-9]+$ && "$parent_pid" != "$process_pid" ]] || break
    process_pid="$parent_pid"
  done
  return 1
}

platform_release_lock_open() {
  platform_release_lock_select_path || return 1
  if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" == "1" ]]; then
    # A supervised body has no lock FD by design.  Its parent /usr/bin/flock
    # owns the descriptor and remains alive until this body exits.
    [[ -z "${PLATFORM_RELEASE_LOCK_FD:-}" ]] || return 1
    platform_release_lock_supervisor_holds || return 1
    RELEASE_LOCK_FD=""
    PLATFORM_RELEASE_LOCK_FD_OWNED=0
    return 0
  fi

  # Inherited numeric FDs are not a supported capability.  Passing one to
  # util-linux `flock --close` makes it a pathname (for example, root file 9),
  # and a raw inherited descriptor cannot prove a live owner in /proc/locks.
  # Callers must re-enter through platform_release_lock_supervise instead.
  [[ -z "${PLATFORM_RELEASE_LOCK_FD:-}" ]] || return 1
  platform_release_lock_prepare || return 1
  exec {RELEASE_LOCK_FD}<>"$PLATFORM_RELEASE_LOCK_PATH" || return 1
  if ! platform_release_lock_matches_path "$RELEASE_LOCK_FD" \
    || ! platform_release_lock_validate_file_metadata "$PLATFORM_RELEASE_LOCK_PATH"; then
    eval "exec ${RELEASE_LOCK_FD}>&-" 2>/dev/null || true
    RELEASE_LOCK_FD=""
    return 1
  fi
  if ! /usr/bin/flock -n "$RELEASE_LOCK_FD"; then
    platform_release_lock_close
    return 1
  fi
  PLATFORM_RELEASE_LOCK_FD_OWNED=1
  # Do not export the descriptor.  Descendant processes must never inherit a
  # lock FD; the top-level util-linux supervisor is the sole owner.
  unset PLATFORM_RELEASE_LOCK_FD
}

platform_release_lock_close() {
  local descriptor="${RELEASE_LOCK_FD:-}"
  if [[ "$descriptor" =~ ^[0-9]+$ && -e "/proc/self/fd/$descriptor" ]]; then
    if [[ "${PLATFORM_RELEASE_LOCK_FD_OWNED:-0}" == "1" ]]; then
      /usr/bin/flock -u "$descriptor" 2>/dev/null || true
    fi
    eval "exec ${descriptor}>&-" 2>/dev/null || true
  fi
  RELEASE_LOCK_FD=""
  PLATFORM_RELEASE_LOCK_FD_OWNED=0
  unset PLATFORM_RELEASE_LOCK_FD
}

platform_retained_load_lock_select_path() {
  PLATFORM_RETAINED_LOAD_LOCK_PATH="$PLATFORM_RETAINED_LOAD_LOCK_CANONICAL_PATH"
}

platform_retained_load_lock_prepare() {
  platform_retained_load_lock_select_path
  local lock_path="$PLATFORM_RETAINED_LOAD_LOCK_PATH"
  # Older retained-load supervisors opened this path with a permissive
  # umask.  Tighten an existing root-owned, single-link regular file before
  # handing it to the common metadata/race checks; never chmod a symlink or a
  # non-root file.
  if [[ -e "$lock_path" && ! -L "$lock_path" ]]; then
    local existing_uid existing_gid existing_links
    existing_uid="$(/usr/bin/stat -c %u -- "$lock_path" 2>/dev/null)" || return 1
    existing_gid="$(/usr/bin/stat -c %g -- "$lock_path" 2>/dev/null)" || return 1
    existing_links="$(/usr/bin/stat -c %h -- "$lock_path" 2>/dev/null)" || return 1
    [[ "$existing_uid:$existing_gid:$existing_links" == "0:0:1" ]] || return 1
    /usr/bin/chmod 0600 -- "$lock_path" || return 1
  fi
  platform_release_lock_create_and_validate "$lock_path" || return 1
}

platform_retained_load_lock_supervisor_holds() {
  platform_retained_load_lock_select_path || return 1
  platform_retained_load_lock_prepare || return 1
  local process_pid="$$" parent_pid
  while [[ "$process_pid" =~ ^[0-9]+$ && "$process_pid" -gt 1 ]]; do
    if platform_retained_load_lock_validate_supervisor_pid "$process_pid"; then
      return 0
    fi
    parent_pid="$(/usr/bin/awk '{print $4}' "/proc/$process_pid/stat" 2>/dev/null || true)"
    [[ "$parent_pid" =~ ^[0-9]+$ && "$parent_pid" != "$process_pid" ]] || break
    process_pid="$parent_pid"
  done
  return 1
}

platform_retained_load_lock_open() {
  platform_retained_load_lock_select_path
  if [[ "${PLATFORM_RETAINED_LOAD_LOCK_SUPERVISED:-}" == "1" ]]; then
    # A pathname-form supervisor closes its descriptor before this body and
    # every child starts.  Validate the live /proc/locks owner instead of
    # accepting an ambient marker or inherited numeric descriptor.
    [[ -z "${PLATFORM_RETAINED_LOAD_LOCK_FD:-}" ]] || return 1
    platform_retained_load_lock_supervisor_holds || return 1
    RETAINED_LOAD_LOCK_FD=""
    RETAINED_LOAD_LOCK_FD_OWNED=0
    return 0
  fi
  [[ -z "${PLATFORM_RETAINED_LOAD_LOCK_FD:-}" ]] || return 1
  platform_retained_load_lock_prepare || return 1
  local lock_path="$PLATFORM_RETAINED_LOAD_LOCK_PATH"
  exec {RETAINED_LOAD_LOCK_FD}<>"$lock_path" || return 1
  if ! platform_release_lock_fd_matches_path \
    "$RETAINED_LOAD_LOCK_FD" "$lock_path" \
    || ! platform_release_lock_validate_file_metadata "$lock_path"; then
    eval "exec ${RETAINED_LOAD_LOCK_FD}>&-" 2>/dev/null || true
    RETAINED_LOAD_LOCK_FD=""
    return 1
  fi
  if ! /usr/bin/flock -n "$RETAINED_LOAD_LOCK_FD"; then
    platform_retained_load_lock_close
    return 1
  fi
  RETAINED_LOAD_LOCK_FD_OWNED=1
}

platform_retained_load_lock_close() {
  local descriptor="${RETAINED_LOAD_LOCK_FD:-}"
  if [[ "$descriptor" =~ ^[0-9]+$ && -e "/proc/self/fd/$descriptor" ]]; then
    if [[ "${RETAINED_LOAD_LOCK_FD_OWNED:-0}" == "1" ]]; then
      /usr/bin/flock -u "$descriptor" 2>/dev/null || true
    fi
    eval "exec ${descriptor}>&-" 2>/dev/null || true
  fi
  RETAINED_LOAD_LOCK_FD=""
  RETAINED_LOAD_LOCK_FD_OWNED=0
}

platform_retained_load_lock_supervise() {
  platform_retained_load_lock_select_path || return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
  if [[ "${PLATFORM_RETAINED_LOAD_LOCK_SUPERVISED:-}" == "1" ]]; then
    [[ -z "${PLATFORM_RETAINED_LOAD_LOCK_FD:-}" ]] || \
      return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
    platform_retained_load_lock_supervisor_holds \
      || return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
    return 0
  fi
  [[ -z "${PLATFORM_RETAINED_LOAD_LOCK_FD:-}" ]] \
    || return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
  platform_retained_load_lock_prepare \
    || return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
  /usr/bin/env -u PLATFORM_RETAINED_LOAD_LOCK_FD \
    PLATFORM_RETAINED_LOAD_LOCK_SUPERVISED=1 \
    /usr/bin/flock -n -E "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE" \
      --close "$PLATFORM_RETAINED_LOAD_LOCK_PATH" "$0" "$@"
}

platform_release_lock_supervise() {
  platform_release_lock_select_path || return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
  if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" == "1" ]]; then
    [[ -z "${PLATFORM_RELEASE_LOCK_FD:-}" ]] || return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
    platform_release_lock_supervisor_holds \
      || return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
    return 0
  fi
  # Never accept an inherited descriptor from ambient environment.  In
  # particular, never pass the numeric value to `flock --close`, where it is
  # interpreted as a filename.
  [[ -z "${PLATFORM_RELEASE_LOCK_FD:-}" ]] \
    || return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
  platform_release_lock_prepare \
    || return "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
  /usr/bin/env -u PLATFORM_RELEASE_LOCK_FD \
    PLATFORM_RELEASE_LOCK_SUPERVISED=1 \
    /usr/bin/flock -n -E "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE" \
      --close "$PLATFORM_RELEASE_LOCK_PATH" "$0" "$@"
}

# Recovery workflows run a short shell body under the same pathname-form
# supervisor without needing to duplicate lock validation inline.  This mode
# is intentionally command-oriented; it has no test pathname or FD override.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -Eeuo pipefail
  case "${1:-}" in
    --run)
      shift
      (( $# > 0 )) || exit "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
      platform_release_lock_prepare \
        || exit "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE"
      exec /usr/bin/env -u PLATFORM_RELEASE_LOCK_FD \
        PLATFORM_RELEASE_LOCK_SUPERVISED=1 \
        /usr/bin/flock -n -E "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE" \
          --close "$PLATFORM_RELEASE_LOCK_PATH" "$@"
      ;;
    *)
      printf '%s\n' 'Usage: platform_release_lock.sh --run COMMAND [ARG...]' >&2
      exit 2
      ;;
  esac
fi
