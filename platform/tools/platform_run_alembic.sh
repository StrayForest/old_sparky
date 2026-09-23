#!/usr/bin/env bash
set -Eeuo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$TOOLS_DIR/platform_runtime_common.sh"

platform_load_env_file
ORIGINAL_ARGS=("$@")

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

  # Repeat the complete release preflight after staging, while the deploy
  # wrapper holds the release lock. This closes the preflight->staging TOCTOU
  # window before the first database mutation.
  "$TOOLS_DIR/platform_release_preflight.sh" \
    --app-dir "$PLATFORM_APP_DIR" \
    --require-previous \
    --require-verified-backup \
    --require-edge-parity \
    --backup-max-age-hours 24

  # No old-code writer may overlap a schema migration. The release wrapper
  # restarts these services only after the candidate pointer is activated, or
  # after a failed command when the durable pre-migration snapshot proves that
  # the original pointers and release identities are still safe. This wrapper
  # itself never starts a writer on an uncertain migration outcome.
  recovery_tool="$PLATFORM_ROOT_DIR/tools/platform_tournament_list_read_model_recovery.py"
  if [[ ! -f "$recovery_tool" || -L "$recovery_tool" ]]; then
    echo "Production Alembic recovery helper is missing or unsafe." >&2
    exit 1
  fi
  /usr/bin/systemctl stop deadlock-api deadlock-worker deadlock-web
  for service in deadlock-api deadlock-worker deadlock-web; do
    if /usr/bin/systemctl is-active --quiet "$service"; then
      echo "Refusing migration while service remains active: $service" >&2
      exit 1
    fi
  done

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
exec "$PLATFORM_PYTHON_BIN" -m alembic "$@"
