#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

# End-to-end release state machine. The low-level installer only stages the
# filesystem candidate; this wrapper owns quiesce/migration/runtime/smoke.

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
ARTIFACT=""
RESUME=0
ABORT_RETAINED=0
CONFIRM_MIGRATION_NOT_REVERSED=0
PUBLIC_RELEASE_SLUG="unavailable"
PUBLIC_SOURCE_SHA="unavailable"
EXPECTED_CSP_MODE="enforce"
EDGE_ORIGIN="https://127.0.0.1"
EDGE_HOST="old-sparky.com"
PUBLIC_EDGE_ORIGIN="https://old-sparky.com"
ORIGINAL_ARGS=("$@")

public_status() {
  local status="$1"
  local class="$2"
  printf 'RELEASE_DEPLOY schema=1 status=%s class=%s release_slug=%s source_sha=%s\n' \
    "$status" "$class" "$PUBLIC_RELEASE_SLUG" "$PUBLIC_SOURCE_SHA"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      APP_DIR="$2"
      shift 2
      ;;
    --artifact)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      ARTIFACT="$2"
      shift 2
      ;;
    --resume)
      RESUME=1
      shift
      ;;
    --abort-retained)
      ABORT_RETAINED=1
      shift
      ;;
    --confirm-migration-not-reversed)
      CONFIRM_MIGRATION_NOT_REVERSED=1
      shift
      ;;
    --expected-csp-mode)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      EXPECTED_CSP_MODE="$2"
      shift 2
      ;;
    --edge-origin)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      EDGE_ORIGIN="$2"
      shift 2
      ;;
    --edge-host)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      EDGE_HOST="$2"
      shift 2
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_release_deploy.sh --artifact <artifact.tar.gz> [--app-dir <path>]
       platform_release_deploy.sh --resume [--app-dir <path>]
       platform_release_deploy.sh --abort-retained \
         --confirm-migration-not-reversed [--app-dir <path>]

Runs the durable production release state machine:
  preflight -> quiesce writers -> stage -> migration decision -> pointer
  activation -> restart/readiness -> Nginx apply -> origin/public smoke
  -> transaction commit.

API/worker writers are stopped before the shared Python runtime can transition
and remain stopped through migration. The web writer and Cloudflare/Nginx timer
are also stopped across that boundary. The receipt records their pre-migration
state; recovery restarts only units that were active before quiesce and retains
the receipt on a state, identity, restart or readiness mismatch. The
authoritative release-independent lock at
/run/lock/oldsparky-platform-release.lock is acquired before preflight and
held through staging, migration, activation and abort/recovery. The
pre-quiesce transaction phase is durably written before the first stop or
staging side effect, so SIGKILL leaves enough state for the guarded abort command. The
transaction is intentionally retained after migration uncertainty; Alembic is
never downgraded automatically.
EOF
      exit 0
      ;;
    *)
      public_status failed argument >&2
      exit 1
      ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  public_status failed privilege >&2
  exit 1
fi
if [[ "$APP_DIR" != /* || "$APP_DIR" == "/" ]]; then
  public_status failed argument >&2
  exit 1
fi
if [[ "$RESUME" -eq 1 && -n "$ARTIFACT" ]]; then
  public_status failed argument >&2
  exit 1
fi
if [[ "$ABORT_RETAINED" -eq 1 && ( "$RESUME" -eq 1 || -n "$ARTIFACT" ) ]]; then
  public_status failed argument >&2
  exit 1
fi
if [[ "$ABORT_RETAINED" -eq 0 && "$RESUME" -eq 0 && -z "$ARTIFACT" ]]; then
  public_status failed argument >&2
  exit 1
fi
if [[ "$ABORT_RETAINED" -eq 1 && "$CONFIRM_MIGRATION_NOT_REVERSED" -eq 0 ]]; then
  public_status failed argument >&2
  exit 1
fi
if [[ "$ABORT_RETAINED" -eq 0 && "$CONFIRM_MIGRATION_NOT_REVERSED" -eq 1 ]]; then
  public_status failed argument >&2
  exit 1
fi
if [[ "$EXPECTED_CSP_MODE" != "enforce" ]]; then
  public_status failed policy >&2
  exit 1
fi

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_TOOL="$TOOLS_DIR/platform_release_install.sh"
TRANSACTION_TOOL="$TOOLS_DIR/platform_release_transaction.py"
RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"
LOCK_HELPER="$TOOLS_DIR/platform_release_lock.sh"
if [[ ! -f "$LOCK_HELPER" || -L "$LOCK_HELPER" ]]; then
  public_status failed lock >&2
  exit 3
fi
# This is the first release side effect.  The lock is independent of the
# release tree so a direct first bootstrap and an already-installed release
# use the exact same lock identity.  A supervised body re-validates its live
# /usr/bin/flock parent; no numeric descriptor is inherited.
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
RELEASE_LOCK_ACQUIRED=1
APP_DIR="$(readlink -f "$APP_DIR" 2>/dev/null || true)"
SHARED_DIR="$APP_DIR/shared"
TRANSACTION_STATE="$SHARED_DIR/.release-operation.json"
QUIESCE_STATE="$TRANSACTION_STATE"
SHARED_VENV="$SHARED_DIR/venv"
WRITERS_QUIESCED=0
SERVICE_STATE_CAPTURED=0
DEADLOCK_API_STATE=""
DEADLOCK_WORKER_STATE=""
DEADLOCK_WEB_STATE=""
CLOUDFLARE_TIMER_STATE=""
CANDIDATE_HINT=""
EXIT_RECOVERY_RUNNING=0

if [[ ! -d "$APP_DIR" || -L "$APP_DIR" || ! -d "$SHARED_DIR" || -L "$SHARED_DIR" ]]; then
  public_status failed layout >&2
  exit 1
fi

transaction_exists() {
  [[ -f "$TRANSACTION_STATE" && ! -L "$TRANSACTION_STATE" ]] \
    && ! quiesce_receipt_exists
}

transaction_path_present() {
  [[ -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]
}

quiesce_receipt_exists() {
  [[ -f "$QUIESCE_STATE" && ! -L "$QUIESCE_STATE" ]] || return 1
  local quiesce_phase
  quiesce_phase="$(
    /usr/bin/python3 -I "$TRANSACTION_TOOL" status \
      --state "$QUIESCE_STATE" --json 2>/dev/null | json_field phase
  )" || return 1
  [[ "$quiesce_phase" == "quiesce-pending" ]]
}

json_field() {
  local field="$1"
  /usr/bin/python3 -I -c \
    'import json,sys; value=json.load(sys.stdin)[sys.argv[1]]; print("" if value is None else value)' \
    "$field" 2>/dev/null
}

transaction_json() {
  /usr/bin/python3 -I "$TRANSACTION_TOOL" status \
    --state "$TRANSACTION_STATE" --json 2>/dev/null
}

transaction_phase() {
  transaction_json | json_field phase
}

quiesce_json() {
  /usr/bin/python3 -I "$TRANSACTION_TOOL" status-quiesce \
    --state "$QUIESCE_STATE" --json 2>/dev/null
}

set_phase() {
  local expected="$1"
  local phase="$2"
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected "$expected" \
    --phase "$phase" >/dev/null 2>/dev/null
}

candidate_env() {
  export PLATFORM_ENV_FILE="$SHARED_DIR/.env.platform"
  export PLATFORM_PYTHON_BIN="$SHARED_VENV/bin/python"
  export PLATFORM_SHARED_DIR="$SHARED_DIR"
  export PLATFORM_APP_DIR="$APP_DIR"
  export PYTHONPATH="$CANDIDATE${PYTHONPATH:+:$PYTHONPATH}"
}

run_candidate() {
  candidate_env
  (cd "$CANDIDATE" && "$@") >/dev/null 2>/dev/null
}

release_preflight() {
  "$TOOLS_DIR/platform_release_preflight.sh" \
    --app-dir "$APP_DIR" \
    --require-previous \
    --require-verified-backup \
    --require-edge-parity \
    --backup-max-age-hours 24
}

print_retained_state() {
  # Durable receipts remain on disk for the guarded recovery commands, but
  # their absolute paths and phase details are private machine state.
  return 0
}

restore_previous_runtime() {
  local release="$1"
  # Runtime preparation must not implicitly enable/start managed timers. The
  # recorded service snapshot below is the only authority for what may start
  # during abort recovery.
  PLATFORM_ENABLE_SYSTEMD_UNITS=0 "$RUNTIME_RESTORE_TOOL" \
    --app-dir "$APP_DIR" \
    --release "$release" \
    --no-restart \
    --expected-csp-mode "$EXPECTED_CSP_MODE" \
    --edge-origin "$EDGE_ORIGIN" \
    --edge-host "$EDGE_HOST" \
    --public-edge-origin "$PUBLIC_EDGE_ORIGIN" >/dev/null 2>/dev/null
}

read_unit_state() {
  local unit="$1"
  local state status
  state=""
  if state="$(/usr/bin/systemctl is-active "$unit" 2>/dev/null)"; then
    status=0
  else
    status="$?"
  fi
  case "$state:$status" in
    active:0|inactive:*)
      printf '%s\n' "$state"
      ;;
    *)
      public_status failed service_state >&2
      return 1
      ;;
  esac
}

require_unit_state() {
  local unit="$1"
  local expected="$2"
  local actual
  actual="$(read_unit_state "$unit")" || return 1
  if [[ "$actual" != "$expected" ]]; then
    public_status failed service_state >&2
    return 1
  fi
}

capture_pre_migration_service_state() {
  if [[ "$SERVICE_STATE_CAPTURED" -eq 1 ]]; then
    return 0
  fi
  DEADLOCK_API_STATE="$(read_unit_state deadlock-api)" || return 1
  DEADLOCK_WORKER_STATE="$(read_unit_state deadlock-worker)" || return 1
  DEADLOCK_WEB_STATE="$(read_unit_state deadlock-web)" || return 1
  CLOUDFLARE_TIMER_STATE="$(read_unit_state deadlock-cloudflare-ips.timer)" || return 1
  SERVICE_STATE_CAPTURED=1
}

load_service_state_from_json() {
  local state_json="$1"
  local -a recorded_service_fields
  readarray -t recorded_service_fields < <(
    printf '%s' "$state_json" | /usr/bin/python3 -I -c '
import json
import sys

record = json.load(sys.stdin)
service_state = record.get("service_state_before")
quiesced = record.get("quiesced_services")
timer_state = record.get("timer_active_before")
if (
    not isinstance(service_state, dict)
    or set(service_state) != {"deadlock-api", "deadlock-worker", "deadlock-web"}
    or quiesced != ["deadlock-api", "deadlock-worker", "deadlock-web"]
    or type(timer_state) is not bool
):
    raise SystemExit("recorded pre-migration service state is unavailable")
for unit in ("deadlock-api", "deadlock-worker", "deadlock-web"):
    value = service_state[unit]
    if type(value) is not str or value not in {"active", "inactive"}:
        raise SystemExit("recorded pre-migration service state is invalid")
    print(value)
print("active" if timer_state else "inactive")
' 2>/dev/null
  )
  if [[ "${#recorded_service_fields[@]}" -ne 4 ]]; then
    public_status failed service_state >&2
    return 1
  fi
  DEADLOCK_API_STATE="${recorded_service_fields[0]}"
  DEADLOCK_WORKER_STATE="${recorded_service_fields[1]}"
  DEADLOCK_WEB_STATE="${recorded_service_fields[2]}"
  CLOUDFLARE_TIMER_STATE="${recorded_service_fields[3]}"
  SERVICE_STATE_CAPTURED=1
}

load_recorded_service_state() {
  load_service_state_from_json "$(transaction_json)"
}

load_quiesce_service_state() {
  load_service_state_from_json "$(quiesce_json)"
}

prepare_quiesce_receipt() {
  [[ "$SERVICE_STATE_CAPTURED" -eq 1 ]] || {
    public_status failed service_state >&2
    return 1
  }
  local -a candidate_flags=()
  if transaction_exists; then
    candidate_flags+=(--candidate-may-exist)
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" prepare-quiesce \
    --state "$QUIESCE_STATE" \
    --app-dir "$APP_DIR" \
    --candidate-release "$CANDIDATE_HINT" \
    "${candidate_flags[@]}" \
    --service-state "deadlock-api=$DEADLOCK_API_STATE" \
    --service-state "deadlock-worker=$DEADLOCK_WORKER_STATE" \
    --service-state "deadlock-web=$DEADLOCK_WEB_STATE" \
    --timer-active-before "$CLOUDFLARE_TIMER_STATE" >/dev/null 2>/dev/null
}

verify_quiesce_receipt() {
  /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-quiesce \
    --state "$QUIESCE_STATE" >/dev/null 2>/dev/null
}

clear_quiesce_receipt() {
  if quiesce_receipt_exists; then
    /usr/bin/python3 -I "$TRANSACTION_TOOL" clear-quiesce \
      --state "$QUIESCE_STATE" >/dev/null 2>/dev/null
  fi
}

restart_recorded_services() {
  [[ "$SERVICE_STATE_CAPTURED" -eq 1 ]] || {
    public_status failed service_state >&2
    return 1
  }
  local service expected
  for service in deadlock-api deadlock-worker deadlock-web; do
    case "$service" in
      deadlock-api) expected="$DEADLOCK_API_STATE" ;;
      deadlock-worker) expected="$DEADLOCK_WORKER_STATE" ;;
      deadlock-web) expected="$DEADLOCK_WEB_STATE" ;;
    esac
    if [[ "$expected" == "active" ]]; then
      /usr/bin/systemctl restart "$service" >/dev/null 2>/dev/null || return 1
      require_unit_state "$service" active || return 1
    else
      /usr/bin/systemctl stop "$service" >/dev/null 2>/dev/null || return 1
      require_unit_state "$service" inactive || return 1
    fi
  done
  if [[ "$CLOUDFLARE_TIMER_STATE" == "active" ]]; then
    /usr/bin/systemctl start deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null || return 1
    require_unit_state deadlock-cloudflare-ips.timer active || return 1
  else
    /usr/bin/systemctl stop deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null || return 1
    require_unit_state deadlock-cloudflare-ips.timer inactive || return 1
  fi
}

verify_recorded_service_readiness() {
  local attempt ready
  for attempt in {1..30}; do
    ready=1
    if [[ "$DEADLOCK_API_STATE" == "active" ]] \
      && ! /usr/bin/curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:8010/api/v1/health/ready >/dev/null 2>/dev/null; then
      ready=0
    fi
    if [[ "$DEADLOCK_WEB_STATE" == "active" ]] \
      && ! /usr/bin/curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:3000/ >/dev/null 2>/dev/null; then
      ready=0
    fi
    if [[ "$ready" -eq 1 ]]; then
      return 0
    fi
    if [[ "$attempt" -eq 30 ]]; then
      public_status failed readiness >&2
      return 1
    fi
    sleep 1
  done
  return 1
}

restore_recorded_services() {
  restart_recorded_services
  verify_recorded_service_readiness
}

acquire_release_lock() {
  if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" == "1" ]]; then
    platform_release_lock_supervisor_holds || {
      public_status failed lock >&2
      exit 3
    }
    return 0
  fi
  if [[ "$RELEASE_LOCK_ACQUIRED" -ne 1 ]]; then
    public_status failed lock >&2
    exit 3
  fi
}

quiesce_runtime_writers() {
  if [[ "$WRITERS_QUIESCED" -eq 1 ]]; then
    return 0
  fi
  local cloudflare_service_state
  if quiesce_receipt_exists; then
    load_quiesce_service_state
    verify_quiesce_receipt
  else
    if transaction_exists; then
      # A staged/resumed transaction already contains the immutable snapshot.
      # It is already durable before this resumed maintenance window, so no
      # second receipt is created (and the staged transaction remains the
      # authoritative lock/identity record).
      load_recorded_service_state
      /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
        --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
    else
      capture_pre_migration_service_state
      prepare_quiesce_receipt
      verify_quiesce_receipt
    fi
  fi
  # The durable receipt is now on disk. Every subsequent stop/stage operation
  # therefore has an explicit service state to recover, including SIGKILL.
  WRITERS_QUIESCED=1
  /usr/bin/systemctl stop deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null
  for attempt in {1..60}; do
    cloudflare_service_state="$(read_unit_state deadlock-cloudflare-ips.service)"
    if [[ "$cloudflare_service_state" == "inactive" ]]; then
      break
    fi
    if [[ "$attempt" -eq 60 ]]; then
      public_status failed quiesce >&2
      return 1
    fi
    sleep 1
  done
  /usr/bin/systemctl stop deadlock-api deadlock-worker deadlock-web >/dev/null 2>/dev/null
  for service in deadlock-api deadlock-worker deadlock-web; do
    if ! require_unit_state "$service" inactive; then
      public_status failed quiesce >&2
      return 1
    fi
  done
  if [[ "$CLOUDFLARE_TIMER_STATE" != "active" \
    && "$CLOUDFLARE_TIMER_STATE" != "inactive" ]]; then
    public_status failed service_state >&2
    return 1
  fi
  return 0
}

original_current_from_transaction() {
  local original_current
  original_current="$(transaction_json | json_field current_before)"
  if [[ -z "$original_current" ]]; then
    original_current="$(transaction_json | json_field previous_before)"
  fi
  printf '%s\n' "$original_current"
}

original_current_from_quiesce() {
  local original_current
  original_current="$(quiesce_json | json_field current_before)"
  if [[ -z "$original_current" ]]; then
    original_current="$(quiesce_json | json_field previous_before)"
  fi
  printf '%s\n' "$original_current"
}

restore_snapshot_runtime() {
  local original_current="$1"
  if [[ -z "$original_current" ]]; then
    public_status failed recovery >&2
    return 1
  fi
  restore_previous_runtime "$original_current"
  restore_recorded_services
}

recover_failure_transaction() {
  local retained_phase="$1"
  case "$retained_phase" in
    prepared|venv-transitioned|snapshot-placed|current-switched|previous-switched|\
    pointers-switched|staged|recovery-authorized|recovery-restored)
      ;;
    *)
      public_status failed recovery >&2
      return 1
      ;;
  esac

  if quiesce_receipt_exists; then
    load_quiesce_service_state
  else
    load_recorded_service_state
  fi
  if [[ "$retained_phase" != "recovery-restored" ]]; then
    /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
      --retain \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
  restore_snapshot_runtime "$(original_current_from_transaction)"
  /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
  if [[ "$retained_phase" != "recovery-restored" ]]; then
    /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
  fi
  clear_quiesce_receipt
}

recover_failure_uncertain_transaction() {
  if quiesce_receipt_exists; then
    load_quiesce_service_state
    verify_quiesce_receipt
    restore_snapshot_runtime "$(original_current_from_quiesce)"
    verify_quiesce_receipt
  else
    load_recorded_service_state
    /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
    restore_snapshot_runtime "$(original_current_from_transaction)"
    /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
  fi
  return 0
}

recover_failure_quiesce_only() {
  load_quiesce_service_state
  verify_quiesce_receipt
  restore_snapshot_runtime "$(original_current_from_quiesce)"
  verify_quiesce_receipt
  /usr/bin/python3 -I "$TRANSACTION_TOOL" abort-quiesce \
    --state "$QUIESCE_STATE" >/dev/null 2>/dev/null
}

recover_after_failure() {
  if [[ "$RELEASE_LOCK_ACQUIRED" -ne 1 ]]; then
    public_status failed lock >&2
    return 1
  fi
  if transaction_path_present && ! transaction_exists \
    && ! quiesce_receipt_exists; then
    public_status failed recovery >&2
    return 1
  fi
  if ! transaction_exists && ! quiesce_receipt_exists; then
    return 0
  fi
  if [[ "$RESUME" -eq 1 ]] && ! transaction_exists; then
    public_status failed recovery >&2
    return 1
  fi
  if transaction_exists; then
    local retained_phase
    local retained_operation
    retained_operation="$(transaction_json | json_field operation)"
    if [[ "$retained_operation" != "install" ]]; then
      public_status failed recovery >&2
      return 1
    fi
    retained_phase="$(transaction_phase)"
    if [[ "$retained_phase" =~ ^(migration-pending|migration-failed|migration-applied)$ ]]; then
      recover_failure_uncertain_transaction
    else
      recover_failure_transaction "$retained_phase"
    fi
  else
    recover_failure_quiesce_only
  fi
}

on_exit() {
  local exit_status="$?"
  local recovery_status=0
  trap - EXIT HUP INT TERM
  if [[ "$EXIT_RECOVERY_RUNNING" -eq 1 ]]; then
    platform_release_lock_close
    exit "$exit_status"
  fi
  EXIT_RECOVERY_RUNNING=1
  if [[ "$exit_status" -ne 0 ]]; then
    set +e
    recover_after_failure
    recovery_status="$?"
    set -e
    if [[ "$recovery_status" -ne 0 ]]; then
      public_status failed recovery >&2
    else
      public_status failed deployment >&2
    fi
  fi
  print_retained_state
  platform_release_lock_close
  exit "$exit_status"
}

trap on_exit EXIT
trap 'exit 130' HUP INT TERM

abort_retained_release() {
  local retained_phase="$1"
  case "$retained_phase" in
    prepared|venv-transitioned|snapshot-placed|current-switched|previous-switched|\
    pointers-switched|staged|migration-pending|migration-failed|migration-applied|activation-pending|\
    services-restarted|nginx-pending|nginx-applied|smoke-passed|\
    activation-committed|recovery-authorized|recovery-restored)
      ;;
    *)
      public_status failed recovery >&2
      return 1
      ;;
  esac
  local original_current
  # Validate the immutable pre-migration snapshot before changing pointers or
  # the venv. An absent/invalid snapshot must not even enter filesystem
  # recovery, because there is no safe service state to restore afterward.
  TRANSACTION_JSON="$(transaction_json)"
  if quiesce_receipt_exists; then
    load_quiesce_service_state
  else
    load_recorded_service_state
  fi
  original_current="$(printf '%s' "$TRANSACTION_JSON" | json_field current_before)"
  if [[ -z "$original_current" ]]; then
    original_current="$(printf '%s' "$TRANSACTION_JSON" | json_field previous_before)"
  fi
  case "$retained_phase" in
    migration-pending|migration-failed|migration-applied|activation-pending|\
    services-restarted|nginx-pending|nginx-applied|smoke-passed|activation-committed)
      /usr/bin/python3 -I "$TRANSACTION_TOOL" authorize-recovery \
        --state "$TRANSACTION_STATE" \
        --confirm MIGRATION_NOT_REVERSED >/dev/null 2>/dev/null
      ;;
    prepared|venv-transitioned|snapshot-placed|current-switched|previous-switched|\
    pointers-switched|staged|recovery-authorized|recovery-restored)
      # No database mutation has been attempted in these phases. A process
      # killed after promotion but before migration can therefore use the
      # same explicit abort command without pretending that a migration was
      # reversed.
      ;;
    *)
    public_status failed recovery >&2
      return 1
      ;;
  esac
  /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
    --retain \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null

  # The transaction tool has restored and identity-checked the original
  # pointers/venv. Restore unit files and Nginx without an unconditional
  # restart, then bring back only the units that were active before quiesce.
  # A missing or changed snapshot therefore fails closed with the receipt
  # retained instead of guessing which services should be started.
  TRANSACTION_JSON="$(transaction_json)"
  if [[ "$(printf '%s' "$TRANSACTION_JSON" | json_field phase)" != "recovery-restored" ]]; then
    public_status failed recovery >&2
    return 1
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
  restore_previous_runtime "$original_current"
  # The pointer check above protects the first recovery step; repeat it after
  # restoring units/Nginx so a concurrent pointer drift cannot authorize a
  # service start. The transaction tool also rechecks the recorded venv
  # identities at this boundary.
  /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
    --retain \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
  restore_recorded_services
  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null
  clear_quiesce_receipt
  public_status passed abort >&2
}

abort_quiesce_receipt() {
  load_quiesce_service_state
  verify_quiesce_receipt
  restore_snapshot_runtime "$(original_current_from_quiesce)"
  verify_quiesce_receipt
  /usr/bin/python3 -I "$TRANSACTION_TOOL" abort-quiesce \
    --state "$QUIESCE_STATE" >/dev/null 2>/dev/null
  public_status passed abort >&2
}

acquire_release_lock

run_release_preflight_quiet() {
  release_preflight
}

if [[ "$ABORT_RETAINED" -eq 1 ]]; then
  if transaction_exists; then
    abort_retained_release "$(transaction_phase)"
  elif quiesce_receipt_exists; then
    abort_quiesce_receipt
  elif transaction_path_present; then
    public_status failed recovery >&2
    exit 1
  else
    public_status failed recovery >&2
    exit 1
  fi
  platform_release_lock_close
  trap - EXIT
  exit 0
fi

if [[ "$RESUME" -eq 0 ]]; then
  if [[ ! -f "$ARTIFACT" || -L "$ARTIFACT" ]]; then
    public_status failed artifact >&2
    exit 1
  fi
  ARTIFACT="$(readlink -f "$ARTIFACT")"
  case "$(basename "$ARTIFACT")" in
    *.tar.gz)
      CANDIDATE_HINT="$APP_DIR/releases/$(basename "$ARTIFACT" .tar.gz)"
      ;;
    *)
      public_status failed artifact >&2
      exit 1
      ;;
  esac
  if transaction_path_present; then
    public_status failed pending_operation >&2
    exit 3
  fi
  run_release_preflight_quiet >/dev/null 2>/dev/null
  quiesce_runtime_writers
  # The candidate installer re-enters through the same pathname-form flock
  # supervisor.  Do not pass a numeric lock FD through the environment: util-
  # linux flock --close treats that value as a pathname and descendants must
  # never inherit the release lock descriptor.
  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"
fi

if ! transaction_exists; then
  if [[ "$RESUME" -eq 1 ]] && quiesce_receipt_exists; then
    public_status failed pending_operation >&2
    exit 3
  fi
  public_status failed pending_operation >&2
  exit 1
fi
TRANSACTION_JSON="$(transaction_json)"
if [[ "$RESUME" -eq 0 ]]; then
  if quiesce_receipt_exists; then
    public_status failed pending_operation >&2
    exit 1
  fi
  load_recorded_service_state
  TRANSACTION_JSON="$(transaction_json)"
fi
CANDIDATE="$(printf '%s' "$TRANSACTION_JSON" | json_field candidate_release)"
CANDIDATE_HINT="$CANDIDATE"
CURRENT_BEFORE="$(printf '%s' "$TRANSACTION_JSON" | json_field current_before)"
if [[ -z "$CANDIDATE" || ! -d "$CANDIDATE" || -L "$CANDIDATE" ]]; then
  public_status failed artifact >&2
  exit 1
fi
CANDIDATE="$(readlink -f "$CANDIDATE")"
candidate_slug="$(basename "$CANDIDATE")"
if [[ ! "$candidate_slug" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
  public_status failed artifact >&2
  exit 1
fi
PUBLIC_RELEASE_SLUG="$candidate_slug"
candidate_sha="$({
  /usr/bin/python3 -I - "$CANDIDATE/RELEASE.json" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="ascii")).get(
        "source_git_commit", ""
    )
except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
    value = ""
if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40,64}", value):
    print(value)
PY
} 2>/dev/null)"
if [[ -n "$candidate_sha" ]]; then
  PUBLIC_SOURCE_SHA="$candidate_sha"
fi

phase="$(printf '%s' "$TRANSACTION_JSON" | json_field phase)"
if [[ "$RESUME" -eq 1 \
  || "$phase" != "prepared" ]]; then
  load_recorded_service_state
fi
if [[ "$RESUME" -eq 1 && ( "$phase" == "staged" \
  || "$phase" == "migration-pending" || "$phase" == "migration-failed" ) ]]; then
  quiesce_runtime_writers
fi

# Re-run the read-only gate while the release lock is held and writers are
# quiesced. This closes the preflight -> stage TOCTOU window before migration.
if [[ "$phase" == "staged" || "$phase" == "migration-pending" || "$phase" == "migration-failed" ]]; then
  release_preflight >/dev/null 2>/dev/null
fi

case "$phase" in
  staged|migration-pending|migration-failed)
    if [[ "$phase" == "staged" ]]; then
      set_phase staged migration-pending
    elif [[ "$phase" == "migration-failed" ]]; then
      :
      set_phase migration-failed migration-pending
    fi
    if run_candidate tools/platform_run_alembic.sh upgrade head >/dev/null 2>/dev/null; then
      :
    else
      migration_status="$?"
      set_phase migration-pending migration-failed
      exit "$migration_status"
    fi
    set_phase migration-pending migration-applied
    phase=migration-applied
    ;;
  migration-applied|activation-pending|services-restarted|nginx-pending|\
  nginx-applied|smoke-passed|activation-committed)
    ;;
  *)
    public_status failed transaction >&2
    exit 1
    ;;
esac

phase="$(transaction_phase)"
if [[ "$phase" == "migration-applied" || "$phase" == "activation-pending" ]]; then
  if [[ -n "$CURRENT_BEFORE" ]]; then
    /usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
      --state "$TRANSACTION_STATE" --name previous --target "$CURRENT_BEFORE" \
      >/dev/null 2>/dev/null
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
    --state "$TRANSACTION_STATE" --name current --target "$CANDIDATE" \
    >/dev/null 2>/dev/null
  if [[ "$phase" == "migration-applied" ]]; then
    set_phase migration-applied activation-pending
  fi
  phase=activation-pending
fi

if [[ "$phase" == "activation-pending" ]]; then
  # The trusted live-QA payload is part of activation identity. Reconcile it
  # while the canonical release lock is still held, before any post-activation
  # readiness or smoke work can observe the new current release.
  LIVE_QA_RUNTIME_INSTALLER="$CANDIDATE/tools/platform_live_qa_runtime_install.py"
  if [[ ! -f "$LIVE_QA_RUNTIME_INSTALLER" || -L "$LIVE_QA_RUNTIME_INSTALLER" ]]; then
    public_status failed liveqa_runtime >&2
    exit 1
  fi
  "$SHARED_VENV/bin/python" -I "$LIVE_QA_RUNTIME_INSTALLER" \
    reconcile --app-dir "$APP_DIR" >/dev/null 2>/dev/null
  # Install units without implicitly starting timers. The recorded snapshot
  # decides which timer is allowed to start below; app units are restarted
  # explicitly after installation.
  PLATFORM_ENABLE_SYSTEMD_UNITS=0 run_candidate tools/platform_install_systemd_units.sh
  /usr/bin/systemctl restart deadlock-api deadlock-worker deadlock-web \
    >/dev/null 2>/dev/null
  for service in deadlock-api deadlock-worker deadlock-web; do
    /usr/bin/systemctl is-active --quiet "$service" >/dev/null 2>/dev/null
  done
  for attempt in {1..30}; do
    if /usr/bin/curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:8010/api/v1/health/ready >/dev/null 2>/dev/null \
      && /usr/bin/curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:3000/ >/dev/null 2>/dev/null; then
      break
    fi
    if [[ "$attempt" -eq 30 ]]; then
      public_status failed readiness >&2
      exit 1
    fi
    sleep 1
  done
  if [[ "$CLOUDFLARE_TIMER_STATE" == "active" ]]; then
    /usr/bin/systemctl start deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null
    /usr/bin/systemctl is-active --quiet deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null
  fi
  WRITERS_QUIESCED=0
  set_phase activation-pending services-restarted
  phase=services-restarted
fi

if [[ "$phase" == "services-restarted" ]]; then
  set_phase services-restarted nginx-pending
  phase=nginx-pending
fi

if [[ "$phase" == "nginx-pending" ]]; then
  candidate_env
  "$SHARED_VENV/bin/python" "$CANDIDATE/tools/platform_install_nginx.py" \
    --json >/dev/null 2>/dev/null
  if ! "$SHARED_VENV/bin/python" "$CANDIDATE/tools/platform_install_nginx.py" \
    --apply --reload --json >/dev/null 2>/dev/null; then
    if [[ -n "$CURRENT_BEFORE" && -f "$CURRENT_BEFORE/tools/platform_install_nginx.py" ]]; then
      :
      if ! "$SHARED_VENV/bin/python" "$CURRENT_BEFORE/tools/platform_install_nginx.py" \
        --apply --reload --json >/dev/null 2>/dev/null; then
        public_status failed recovery >&2
      fi
    fi
    exit 1
  fi
  set_phase nginx-pending nginx-applied
  phase=nginx-applied
fi

if [[ "$phase" == "nginx-applied" ]]; then
  candidate_env
  "$SHARED_VENV/bin/python" "$CANDIDATE/tools/platform_deploy_smoke.py" \
    --app-dir "$APP_DIR" \
    --env-file "$SHARED_DIR/.env.platform" \
    --edge-origin "$EDGE_ORIGIN" \
    --edge-host "$EDGE_HOST" \
    --edge-insecure-loopback \
    --expected-csp-mode "$EXPECTED_CSP_MODE" >/dev/null 2>/dev/null
  "$SHARED_VENV/bin/python" "$CANDIDATE/tools/platform_deploy_smoke.py" \
    --app-dir "$APP_DIR" \
    --env-file "$SHARED_DIR/.env.platform" \
    --edge-origin "$PUBLIC_EDGE_ORIGIN" \
    --expected-csp-mode "$EXPECTED_CSP_MODE" >/dev/null 2>/dev/null
  release_preflight
  set_phase nginx-applied smoke-passed
  phase=smoke-passed
fi

if [[ "$phase" == "smoke-passed" ]]; then
  set_phase smoke-passed activation-committed
  phase=activation-committed
fi

if [[ "$phase" == "activation-committed" ]]; then
  clear_quiesce_receipt
  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"

fi

platform_release_lock_close
trap - EXIT
public_status passed deployment
