#!/usr/bin/env bash
set -Eeuo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$TOOLS_DIR/platform_runtime_common.sh"

platform_load_env_file
ORIGINAL_ARGS=("$@")
SYSTEMCTL_BIN="${PLATFORM_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"
ALEMBIC_TIMEOUT_BIN="/usr/bin/timeout"
ALEMBIC_OPERATION_TIMEOUT_SECONDS="${PLATFORM_ALEMBIC_OPERATION_TIMEOUT_SECONDS:-300}"

[[ "$ALEMBIC_OPERATION_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,3}$ ]] \
  && (( ALEMBIC_OPERATION_TIMEOUT_SECONDS <= 600 )) || {
  echo "Production Alembic operation timeout is invalid." >&2
  exit 1
}

# The release preflight, partial-0051 repair and final Alembic upgrade are one
# durable migration operation.  Put the complete production path behind one
# process-group deadline before any lock, service or database work begins;
# migration-pending remains authoritative when timeout(1) returns 124.
if [[ "${PLATFORM_ENVIRONMENT:-}" == "production" \
  && "$#" -eq 2 && "$1" == "upgrade" && "$2" == "head" \
  && "${PLATFORM_ALEMBIC_OPERATION_GUARDED:-}" != "1" ]]; then
  export PLATFORM_ALEMBIC_OPERATION_GUARDED=1
  exec "$ALEMBIC_TIMEOUT_BIN" --signal=TERM --kill-after=10s \
    "${ALEMBIC_OPERATION_TIMEOUT_SECONDS}s" \
    "$TOOLS_DIR/platform_run_alembic.sh" "$@"
fi

[[ "$SYSTEMCTL_BIN" == /* && "$SYSTEMCTL_BIN" != *$'\n'* ]] || {
  echo "Production Alembic systemctl path is invalid." >&2
  exit 1
}
[[ -f "$SYSTEMCTL_BIN" && ! -L "$SYSTEMCTL_BIN" ]] || {
  echo "Production Alembic systemctl path is unavailable." >&2
  exit 1
}
systemctl_metadata="$(/usr/bin/stat -c '%F:%u:%g:%h:%a' -- "$SYSTEMCTL_BIN" 2>/dev/null || true)"
[[ "$systemctl_metadata" == "regular file:0:0:1:755" ]] || {
  echo "Production Alembic systemctl path metadata is unsafe." >&2
  exit 1
}
[[ "$(/usr/bin/readlink -f -- "$SYSTEMCTL_BIN" 2>/dev/null || true)" == "$SYSTEMCTL_BIN" ]] || {
  echo "Production Alembic systemctl path must not resolve through a link." >&2
  exit 1
}

run_systemctl() {
  "$SYSTEMCTL_TIMEOUT_BIN" --signal=TERM --kill-after=5s 30s "$SYSTEMCTL_BIN" "$@"
}

read_inactive_state() {
  local service="$1" output status
  if output="$(run_systemctl is-active "$service" 2>/dev/null)"; then
    status=0
  else
    status=$?
  fi
  [[ "$status" -eq 3 && "$output" == "inactive" ]]
}

is_production_upgrade=0
if [[ "${PLATFORM_ENVIRONMENT:-}" == "production" \
  && $# -eq 2 && "$1" == "upgrade" && "$2" == "head" ]]; then
  is_production_upgrade=1
fi

if [[ "${PLATFORM_ENVIRONMENT:-}" == "production" \
  && "$is_production_upgrade" -ne 1 ]]; then
  echo "Production Alembic permits only the exact command: upgrade head." >&2
  exit 2
fi

platform_require_python

if [[ "$is_production_upgrade" -eq 1 ]]; then
  if [[ "$EUID" -ne 0 ]]; then
    echo "Production Alembic upgrade must run as root inside the release transaction." >&2
    exit 1
  fi
  lock_helper="$TOOLS_DIR/platform_release_lock.sh"
  if [[ ! -f "$lock_helper" || -L "$lock_helper" ]]; then
    echo "Production Alembic upgrade requires the canonical release lock helper." >&2
    exit 3
  fi
  # The deploy wrapper exports this descriptor. Direct production invocation
  # acquires the same lock, so no service stop or database access can occur
  # outside the release lock boundary and nested callers never deadlock.
  # shellcheck source=/dev/null
  source "$lock_helper"
  platform_release_lock_supervise "${ORIGINAL_ARGS[@]}" || {
    lock_status=$?
    if [[ "$lock_status" -eq "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE" ]]; then
      echo "Production Alembic could not acquire the canonical release lock." >&2
      exit 3
    fi
    exit "$lock_status"
  }
  if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" != "1" ]]; then
    exit 0
  fi
  if ! platform_release_lock_open; then
    echo "Production Alembic upgrade could not acquire the canonical release lock." >&2
    exit 3
  fi
  trap platform_release_lock_close EXIT
  transaction_state="$PLATFORM_APP_DIR/shared/.release-operation.json"
  transaction_tool="$PLATFORM_ROOT_DIR/tools/platform_release_transaction.py"
  if [[ ! -f "$transaction_state" || -L "$transaction_state" ]]; then
    echo "Production Alembic upgrade requires a durable release transaction." >&2
    exit 1
  fi
  transaction_json="$(
    /usr/bin/python3 -I "$transaction_tool" status \
      --state "$transaction_state" --json
  )"
  readarray -t transaction_fields < <(
    printf '%s' "$transaction_json" | /usr/bin/python3 -I -c '
import json
import sys
record = json.load(sys.stdin)
print(record["operation"])
print(record["phase"])
print(record["app_dir"])
print(record["current_before"] or "")
print(record["previous_before"] or "")
service_state = record.get("service_state_before")
quiesced = record.get("quiesced_services")
timer_state = record.get("timer_active_before")
if (
    not isinstance(service_state, dict)
    or set(service_state) != {"deadlock-api", "deadlock-worker", "deadlock-web"}
    or any(
        type(value) is not str or value not in {"active", "inactive"}
        for value in service_state.values()
    )
    or quiesced != ["deadlock-api", "deadlock-worker", "deadlock-web"]
    or type(timer_state) is not bool
):
    print("invalid")
else:
    print("valid")
print(record.get("candidate_release") or "")
'
  )
  if [[ "${transaction_fields[0]:-}" != "install" \
    || "${transaction_fields[1]:-}" != "migration-pending" \
    || "${transaction_fields[2]:-}" != "$PLATFORM_APP_DIR" \
    || "${transaction_fields[5]:-}" != "valid" ]]; then
    echo "Production Alembic upgrade is outside the migration-pending install phase." >&2
    exit 1
  fi

  read_pointer_target() {
    local pointer="$1"
    if [[ -L "$pointer" ]]; then
      readlink -f "$pointer"
    elif [[ -e "$pointer" ]]; then
      return 1
    else
      printf '%s\n' ""
    fi
  }
  current_pointer="$(read_pointer_target "$PLATFORM_APP_DIR/current")" || {
    echo "Production Alembic current pointer is unsafe." >&2
    exit 1
  }
  previous_pointer="$(read_pointer_target "$PLATFORM_APP_DIR/previous")" || {
    echo "Production Alembic previous pointer is unsafe." >&2
    exit 1
  }
  if [[ "$current_pointer" != "${transaction_fields[3]:-}" \
    || "$previous_pointer" != "${transaction_fields[4]:-}" ]]; then
    echo "Production Alembic release pointers do not match the transaction." >&2
    exit 1
  fi
  candidate_root="$(readlink -f -- "$PLATFORM_ROOT_DIR" 2>/dev/null || true)"
  if [[ -z "$candidate_root" || "$candidate_root" != "${transaction_fields[6]:-}" ]]; then
    echo "Production Alembic candidate does not match the release transaction." >&2
    exit 1
  fi

  # Repeat the complete release preflight after staging, while the deploy
  # wrapper holds the release lock. This closes the preflight->staging TOCTOU
  # window before the first database mutation.
  migration_preflight_previous_flag=(--require-previous)
  if [[ -z "${transaction_fields[4]:-}" ]]; then
    if [[ -z "${transaction_fields[3]:-}" ]]; then
      migration_preflight_previous_flag=(--allow-initial-install)
    else
      migration_preflight_previous_flag=(--allow-no-previous)
    fi
  fi
  "$TOOLS_DIR/platform_release_preflight.sh" \
    --app-dir "$PLATFORM_APP_DIR" \
    "${migration_preflight_previous_flag[@]}" \
    --defer-active-alembic-revision-check \
    --require-verified-backup \
    --require-edge-parity \
    --backup-max-age-hours 24

  # No old-code writer may overlap a schema migration.  A first install has
  # no old release (both durable pointers are absent), so it has no old
  # service topology to quiesce and must not issue host-level systemd calls.
  # Existing-release upgrades retain the stop/verify boundary below; the
  # release wrapper restarts services only after pointer activation, or after
  # a failed command when the durable pre-migration snapshot proves that the
  # original pointers and release identities are still safe.
  recovery_tool="$PLATFORM_ROOT_DIR/tools/platform_tournament_list_read_model_recovery.py"
  if [[ ! -f "$recovery_tool" || -L "$recovery_tool" ]]; then
    echo "Production Alembic recovery helper is missing or unsafe." >&2
    exit 1
  fi
  if [[ -n "${transaction_fields[3]:-}" || -n "${transaction_fields[4]:-}" ]]; then
    run_systemctl stop deadlock-api deadlock-worker deadlock-web
    for service in deadlock-api deadlock-worker deadlock-web; do
      if ! read_inactive_state "$service"; then
        echo "Refusing migration without an exact inactive service state: $service" >&2
        exit 1
      fi
    done
  fi

  # The active release graph may be older than the authenticated candidate.
  # At this point the candidate runtime is selected, writers are quiesced and
  # the exact install transaction/pointers were verified above. Validate the
  # live database revision against that candidate's sole forward graph before
  # recovery helpers or Alembic can write anything.
  migration_guard="$PLATFORM_ROOT_DIR/tools/platform_release_migration_guard.py"
  if [[ ! -f "$migration_guard" || -L "$migration_guard" ]]; then
    echo "Production candidate migration guard is missing or unsafe." >&2
    exit 1
  fi
  migration_guard_args=(
    --candidate-dir "$PLATFORM_ROOT_DIR"
  )
  if [[ -z "${transaction_fields[3]:-}" && -z "${transaction_fields[4]:-}" ]]; then
    migration_guard_args+=(--allow-empty-database)
  fi
  if ! "$SYSTEMCTL_TIMEOUT_BIN" --signal=TERM --kill-after=5s 30s \
    "$PLATFORM_PYTHON_BIN" -I -B "$migration_guard" \
    "${migration_guard_args[@]}"; then
    echo "Production candidate migration path validation failed." >&2
    exit 1
  fi

  # 0051 commits its table/backfill before concurrent indexes.  Repair only
  # that exact, validated partial state before Alembic is allowed to proceed;
  # this preserves the production upgrade-head-only and transaction guards.
  (
    cd "$PLATFORM_ROOT_DIR"
    "$PLATFORM_PYTHON_BIN" "$recovery_tool" \
      --recover-partial
  )
fi

cd "$PLATFORM_ROOT_DIR"
# The complete production path is already inside the process-group deadline
# above.  DB-level connect/lock/statement bounds are supplied by the recovery
# helper and preflight engine; this final command never retries or downgrades.
exec "$PLATFORM_PYTHON_BIN" -m alembic "$@"
