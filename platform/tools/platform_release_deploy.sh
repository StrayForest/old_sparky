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
SYSTEMCTL_BIN="/usr/bin/systemctl"
SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"
SYSTEMD_CALL_TIMEOUT_SECONDS=30
SYSTEMD_OPERATION_TIMEOUT_SECONDS=120
SYSTEMD_OPERATION_DEADLINE_NS=""
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
SERVICE_ENABLEMENT_CAPTURED=0
DEADLOCK_API_STATE=""
DEADLOCK_WORKER_STATE=""
DEADLOCK_WEB_STATE=""
CLOUDFLARE_TIMER_STATE=""
CLOUDFLARE_TIMER_ENABLED=""
DEADLOCK_API_ENABLED=""
DEADLOCK_WORKER_ENABLED=""
DEADLOCK_WEB_ENABLED=""
CANDIDATE_HINT=""
EXIT_RECOVERY_RUNNING=0
INITIAL_INSTALL=0
CURRENT_ONLY_INSTALL=0

if [[ ! -d "$APP_DIR" || -L "$APP_DIR" || ! -d "$SHARED_DIR" || -L "$SHARED_DIR" ]]; then
  public_status failed layout >&2
  exit 1
fi

read_pointer_target() {
  local pointer="$1"
  if [[ -L "$pointer" ]]; then
    readlink -f "$pointer" 2>/dev/null || return 1
  elif [[ -e "$pointer" ]]; then
    return 1
  else
    printf '%s\n' ""
  fi
}

INITIAL_CURRENT_TARGET="$(read_pointer_target "$APP_DIR/current")" || {
  public_status failed layout >&2
  exit 1
}
INITIAL_PREVIOUS_TARGET="$(read_pointer_target "$APP_DIR/previous")" || {
  public_status failed layout >&2
  exit 1
}
if [[ -z "$INITIAL_CURRENT_TARGET" && -n "$INITIAL_PREVIOUS_TARGET" ]]; then
  public_status failed layout >&2
  exit 1
elif [[ -z "$INITIAL_CURRENT_TARGET" && -z "$INITIAL_PREVIOUS_TARGET" ]]; then
  INITIAL_INSTALL=1
elif [[ -n "$INITIAL_CURRENT_TARGET" && -z "$INITIAL_PREVIOUS_TARGET" ]]; then
  CURRENT_ONLY_INSTALL=1
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

set_initial_install_from_transaction() {
  local operation current_before
  operation="$(printf '%s' "$TRANSACTION_JSON" | json_field operation)"
  current_before="$(printf '%s' "$TRANSACTION_JSON" | json_field current_before)"
  if [[ "$operation" == "install" && -z "$current_before" ]]; then
    INITIAL_INSTALL=1
    CURRENT_ONLY_INSTALL=0
  fi
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

systemd_now_ns() {
  local now
  now="$(date +%s%N 2>/dev/null)" || return 1
  [[ "$now" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$now"
}

systemd_operation_begin() {
  local now
  now="$(systemd_now_ns)" || return 1
  SYSTEMD_OPERATION_DEADLINE_NS=$((now + SYSTEMD_OPERATION_TIMEOUT_SECONDS * 1000000000))
}

systemd_operation_reset() {
  SYSTEMD_OPERATION_DEADLINE_NS=""
  systemd_operation_begin
}

systemd_operation_end() {
  SYSTEMD_OPERATION_DEADLINE_NS=""
}

systemd_call_timeout() {
  local call_limit="${1:-$SYSTEMD_CALL_TIMEOUT_SECONDS}"
  local now remaining budget seconds millis
  [[ "$call_limit" =~ ^[0-9]+$ ]] || return 1
  [[ -n "$SYSTEMD_OPERATION_DEADLINE_NS" ]] || {
    printf '%ss\n' "$call_limit"
    return 0
  }
  now="$(systemd_now_ns)" || return 1
  remaining=$((SYSTEMD_OPERATION_DEADLINE_NS - now))
  (( remaining > 0 )) || return 1
  budget=$((call_limit * 1000000000))
  (( remaining < budget )) && budget=$remaining
  seconds=$((budget / 1000000000))
  millis=$(((budget % 1000000000 + 999999) / 1000000))
  if (( millis >= 1000 )); then
    seconds=$((seconds + 1))
    millis=0
  fi
  printf '%d.%03ds\n' "$seconds" "$millis"
}

run_bounded_command() {
  local call_limit="$1"
  shift
  local timeout_value
  timeout_value="$(systemd_call_timeout "$call_limit")" || return 1
  "$SYSTEMCTL_TIMEOUT_BIN" --signal=TERM --kill-after=5s \
    "$timeout_value" "$@"
}

run_systemd_bounded() {
  run_bounded_command "$SYSTEMD_CALL_TIMEOUT_SECONDS" "$@"
}

run_initial_systemd_transaction() {
  run_systemd_bounded /usr/bin/python3 -I "$TRANSACTION_TOOL" "$@" \
    >/dev/null 2>/dev/null
}

run_initial_systemd_candidate() {
  candidate_env
  run_systemd_bounded /bin/bash -c \
    'cd -- "$1" && shift && exec "$@"' _ "$CANDIDATE" "$@" \
    >/dev/null 2>/dev/null
}

run_live_qa_reconcile() {
  run_systemd_bounded "$SHARED_VENV/bin/python" -I "$LIVE_QA_RUNTIME_INSTALLER" \
    reconcile --app-dir "$APP_DIR" >/dev/null
}

wait_for_activation_readiness() {
  local attempt
  for attempt in {1..30}; do
    if run_bounded_command 5 /usr/bin/curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:8010/api/v1/health/ready >/dev/null 2>/dev/null \
      && run_bounded_command 5 /usr/bin/curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:3000/ >/dev/null 2>/dev/null; then
      return 0
    fi
    if [[ "$attempt" -eq 30 ]]; then
      return 1
    fi
    run_bounded_command 1 /bin/sleep 1 >/dev/null 2>/dev/null || return 1
  done
}

release_preflight() {
  local active_revision_flag=()
  if [[ "${1:-}" == "--defer-active-alembic-revision-check" ]]; then
    active_revision_flag=(--defer-active-alembic-revision-check)
  elif [[ -n "${1:-}" ]]; then
    public_status failed argument >&2
    return 1
  fi
  local previous_flag=(--require-previous)
  if [[ "$INITIAL_INSTALL" -eq 1 ]]; then
    previous_flag=(--allow-initial-install)
  elif [[ "$CURRENT_ONLY_INSTALL" -eq 1 ]]; then
    previous_flag=(--allow-no-previous)
  fi
  if transaction_path_present; then
    local transaction_previous transaction_current
    transaction_previous="$(transaction_json | json_field previous_before)"
    transaction_current="$(transaction_json | json_field current_before)"
    if [[ -z "$transaction_previous" ]]; then
      if [[ -z "$transaction_current" ]]; then
        previous_flag=(--allow-initial-install)
      else
        previous_flag=(--allow-no-previous)
      fi
    else
      previous_flag=(--require-previous)
    fi
  fi
  "$TOOLS_DIR/platform_release_preflight.sh" \
    --app-dir "$APP_DIR" \
    "${active_revision_flag[@]}" \
    "${previous_flag[@]}" \
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

# Keep every mutable release-side systemd operation bounded.  Recovery never
# relies on a shell's inherited timeout or on systemd returning promptly; a
# wedged manager must leave the durable receipt in place for retry. During the
# clean-install activation boundary, SYSTEMD_OPERATION_DEADLINE_NS bounds the
# complete reconcile/restore/installer/enable/readiness/verify sequence, while
# each individual systemd, installer, probe or wait call is explicitly capped.
run_systemctl() {
  run_systemd_bounded "$SYSTEMCTL_BIN" "$@"
}

read_unit_state() {
  local unit="$1"
  local state status
  state=""
  if state="$(run_systemctl is-active "$unit" 2>/dev/null)"; then
    status=0
  else
    status="$?"
  fi
  case "$state:$status" in
    active:0|inactive:3)
      printf '%s\n' "$state"
      ;;
    *)
      public_status failed service_state >&2
      return 1
      ;;
  esac
}

# Cloudflare's refresh unit is a oneshot.  A previous failed oneshot can still
# make `systemctl is-active` return `failed:3` after systemd has removed its
# cgroup.  Treat that exact, process-free state as quiescent without weakening
# the generic service-state grammar used by the runtime services and timer.
cloudflare_failed_oneshot_is_empty() {
  local properties line key value
  local -A observed=()
  if ! properties="$(run_systemctl show deadlock-cloudflare-ips.service \
    --property=ActiveState,SubState,Type,RemainAfterExit,KillMode,MainPID,ControlPID,ControlGroup \
    2>/dev/null)"; then
    return 1
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == *=* ]] || return 1
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      ActiveState|SubState|Type|RemainAfterExit|KillMode|MainPID|ControlPID|ControlGroup) ;;
      *) return 1 ;;
    esac
    [[ ! -v "observed[$key]" ]] || return 1
    observed["$key"]="$value"
  done <<<"$properties"
  [[ "${#observed[@]}" -eq 8 \
    && "${observed[ActiveState]}" == "failed" \
    && "${observed[SubState]}" == "failed" \
    && "${observed[Type]}" == "oneshot" \
    && "${observed[RemainAfterExit]}" == "no" \
    && "${observed[KillMode]}" == "control-group" \
    && -z "${observed[ControlGroup]}" \
    && "${observed[MainPID]}" == "0" \
    && "${observed[ControlPID]}" == "0" ]]
}

read_cloudflare_quiescence_state() {
  local state status
  state=""
  if state="$(run_systemctl is-active deadlock-cloudflare-ips.service 2>/dev/null)"; then
    status=0
  else
    status="$?"
  fi
  case "$state:$status" in
    active:0|inactive:3)
      printf '%s\n' "$state"
      ;;
    failed:3)
      if cloudflare_failed_oneshot_is_empty; then
        printf 'inactive\n'
      else
        public_status failed service_state >&2
        return 1
      fi
      ;;
    *)
      public_status failed service_state >&2
      return 1
      ;;
  esac
}

read_unit_enabled() {
  local unit="$1"
  local state status
  state=""
  if state="$(run_systemctl is-enabled "$unit" 2>/dev/null)"; then
    status=0
  else
    status="$?"
  fi
  case "$state:$status" in
    enabled:0|disabled:1)
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

require_unit_enabled() {
  local unit="$1"
  local actual
  actual="$(read_unit_enabled "$unit")" || return 1
  if [[ "$actual" != "enabled" ]]; then
    public_status failed service_state >&2
    return 1
  fi
}

capture_initial_systemd_baseline() {
  [[ "$INITIAL_INSTALL" -eq 1 ]] || return 0
  local owns_deadline=0 status
  if [[ -z "$SYSTEMD_OPERATION_DEADLINE_NS" ]]; then
    systemd_operation_begin || return 1
    owns_deadline=1
  fi
  if run_initial_systemd_transaction capture-initial-systemd \
    --state "$TRANSACTION_STATE" \
    --systemctl "$SYSTEMCTL_BIN"; then
    status=0
  else
    status=$?
  fi
  if [[ "$owns_deadline" -eq 1 ]]; then
    systemd_operation_end
  fi
  return "$status"
}

restore_initial_systemd_baseline() {
  [[ "$INITIAL_INSTALL" -eq 1 ]] || return 0
  local owns_deadline=0 status
  if [[ -z "$SYSTEMD_OPERATION_DEADLINE_NS" ]]; then
    systemd_operation_begin || return 1
    owns_deadline=1
  fi
  if run_initial_systemd_transaction restore-initial-systemd \
    --state "$TRANSACTION_STATE" \
    --systemctl "$SYSTEMCTL_BIN"; then
    status=0
  else
    status=$?
  fi
  if [[ "$owns_deadline" -eq 1 ]]; then
    systemd_operation_end
  fi
  return "$status"
}

verify_initial_systemd_baseline() {
  [[ "$INITIAL_INSTALL" -eq 1 ]] || return 0
  local owns_deadline=0 status
  if [[ -z "$SYSTEMD_OPERATION_DEADLINE_NS" ]]; then
    systemd_operation_begin || return 1
    owns_deadline=1
  fi
  if run_initial_systemd_transaction verify-initial-systemd \
    --state "$TRANSACTION_STATE" \
    --systemctl "$SYSTEMCTL_BIN"; then
    status=0
  else
    status=$?
  fi
  if [[ "$owns_deadline" -eq 1 ]]; then
    systemd_operation_end
  fi
  return "$status"
}

validate_initial_systemd_receipt() {
  [[ "$INITIAL_INSTALL" -eq 1 ]] || return 0
  /usr/bin/python3 -I "$TRANSACTION_TOOL" validate-initial-systemd \
    --state "$TRANSACTION_STATE" \
    --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null
}

verify_initial_systemd_activation() {
  [[ "$INITIAL_INSTALL" -eq 1 ]] || return 0
  local owns_deadline=0 status
  if [[ -z "$SYSTEMD_OPERATION_DEADLINE_NS" ]]; then
    systemd_operation_begin || return 1
    owns_deadline=1
  fi
  if run_initial_systemd_transaction verify-initial-systemd-activated \
    --state "$TRANSACTION_STATE" \
    --systemctl "$SYSTEMCTL_BIN"; then
    status=0
  else
    status=$?
  fi
  if [[ "$owns_deadline" -eq 1 ]]; then
    systemd_operation_end
  fi
  return "$status"
}

restore_initial_systemd_with_fresh_deadline() {
  systemd_operation_reset || return 1
  local status=0
  restore_initial_systemd_baseline || status=$?
  systemd_operation_end
  return "$status"
}

verify_initial_systemd_with_fresh_deadline() {
  systemd_operation_reset || return 1
  local status=0
  verify_initial_systemd_baseline || status=$?
  systemd_operation_end
  return "$status"
}

rewind_initial_systemd_phase_for_retry() {
  [[ "$INITIAL_INSTALL" -eq 1 ]] || return 0
  local retained_phase
  retained_phase="$(transaction_json | json_field phase)"
  case "$retained_phase" in
    systemd-activated|activation-committed)
      set_phase "$retained_phase" systemd-activation-pending
      ;;
  esac
}

capture_pre_migration_service_state() {
  if [[ "$SERVICE_STATE_CAPTURED" -eq 1 ]]; then
    return 0
  fi
  if [[ "$INITIAL_INSTALL" -eq 1 ]]; then
    DEADLOCK_API_STATE="inactive"
    DEADLOCK_WORKER_STATE="inactive"
    DEADLOCK_WEB_STATE="inactive"
    CLOUDFLARE_TIMER_STATE="inactive"
    DEADLOCK_API_ENABLED="disabled"
    DEADLOCK_WORKER_ENABLED="disabled"
    DEADLOCK_WEB_ENABLED="disabled"
    CLOUDFLARE_TIMER_ENABLED="disabled"
  else
    DEADLOCK_API_STATE="$(read_unit_state deadlock-api)" || return 1
    DEADLOCK_WORKER_STATE="$(read_unit_state deadlock-worker)" || return 1
    DEADLOCK_WEB_STATE="$(read_unit_state deadlock-web)" || return 1
    CLOUDFLARE_TIMER_STATE="$(read_unit_state deadlock-cloudflare-ips.timer)" || return 1
    DEADLOCK_API_ENABLED="$(read_unit_enabled deadlock-api)" || return 1
    DEADLOCK_WORKER_ENABLED="$(read_unit_enabled deadlock-worker)" || return 1
    DEADLOCK_WEB_ENABLED="$(read_unit_enabled deadlock-web)" || return 1
    CLOUDFLARE_TIMER_ENABLED="$(read_unit_enabled deadlock-cloudflare-ips.timer)" || return 1
  fi
  SERVICE_STATE_CAPTURED=1
}

load_service_state_from_json() {
  local state_json="$1"
  local allow_legacy="${2:-0}"
  local -a recorded_service_fields
  readarray -t recorded_service_fields < <(
    printf '%s' "$state_json" | /usr/bin/python3 -I -c '
import json
import sys

allow_legacy = sys.argv[1] == "1"
record = json.load(sys.stdin)
service_state = record.get("service_state_before")
quiesced = record.get("quiesced_services")
timer_state = record.get("timer_active_before")
service_enabled = record.get("service_enabled_before")
timer_enabled = record.get("timer_enabled_before")
if allow_legacy and service_enabled is None and timer_enabled is None:
    if (
        not isinstance(service_state, dict)
        or set(service_state) != {"deadlock-api", "deadlock-worker", "deadlock-web"}
        or quiesced != ["deadlock-api", "deadlock-worker", "deadlock-web"]
        or type(timer_state) is not bool
    ):
        raise SystemExit("legacy recorded pre-migration service state is unavailable")
    for unit in ("deadlock-api", "deadlock-worker", "deadlock-web"):
        value = service_state[unit]
        if type(value) is not str or value not in {"active", "inactive"}:
            raise SystemExit("legacy recorded pre-migration service state is invalid")
        print(value)
    print("active" if timer_state else "inactive")
    raise SystemExit(0)
if (
    not isinstance(service_state, dict)
    or set(service_state) != {"deadlock-api", "deadlock-worker", "deadlock-web"}
    or quiesced != ["deadlock-api", "deadlock-worker", "deadlock-web"]
    or type(timer_state) is not bool
    or not isinstance(service_enabled, dict)
    or set(service_enabled) != {"deadlock-api", "deadlock-worker", "deadlock-web"}
    or any(type(value) is not str or value not in {"enabled", "disabled"} for value in service_enabled.values())
    or timer_enabled not in {"enabled", "disabled"}
):
    raise SystemExit("recorded pre-migration service state is unavailable")
for unit in ("deadlock-api", "deadlock-worker", "deadlock-web"):
    value = service_state[unit]
    if type(value) is not str or value not in {"active", "inactive"}:
        raise SystemExit("recorded pre-migration service state is invalid")
    print(value)
print("active" if timer_state else "inactive")
for unit in ("deadlock-api", "deadlock-worker", "deadlock-web"):
    print(service_enabled[unit])
print(timer_enabled)
' "$allow_legacy" 2>/dev/null
  )
  if [[ "$allow_legacy" == "1" && "${#recorded_service_fields[@]}" -eq 4 ]]; then
    DEADLOCK_API_STATE="${recorded_service_fields[0]}"
    DEADLOCK_WORKER_STATE="${recorded_service_fields[1]}"
    DEADLOCK_WEB_STATE="${recorded_service_fields[2]}"
    CLOUDFLARE_TIMER_STATE="${recorded_service_fields[3]}"
    DEADLOCK_API_ENABLED=""
    DEADLOCK_WORKER_ENABLED=""
    DEADLOCK_WEB_ENABLED=""
    CLOUDFLARE_TIMER_ENABLED=""
    SERVICE_ENABLEMENT_CAPTURED=0
  elif [[ "${#recorded_service_fields[@]}" -ne 8 ]]; then
    public_status failed service_state >&2
    return 1
  else
    DEADLOCK_API_STATE="${recorded_service_fields[0]}"
    DEADLOCK_WORKER_STATE="${recorded_service_fields[1]}"
    DEADLOCK_WEB_STATE="${recorded_service_fields[2]}"
    CLOUDFLARE_TIMER_STATE="${recorded_service_fields[3]}"
    DEADLOCK_API_ENABLED="${recorded_service_fields[4]}"
    DEADLOCK_WORKER_ENABLED="${recorded_service_fields[5]}"
    DEADLOCK_WEB_ENABLED="${recorded_service_fields[6]}"
    CLOUDFLARE_TIMER_ENABLED="${recorded_service_fields[7]}"
    SERVICE_ENABLEMENT_CAPTURED=1
  fi
  SERVICE_STATE_CAPTURED=1
}

load_recorded_service_state() {
  load_service_state_from_json "$(transaction_json)"
}

load_quiesce_service_state() {
  load_service_state_from_json "$(quiesce_json)" 1
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
    --timer-active-before "$CLOUDFLARE_TIMER_STATE" \
    --service-enabled "deadlock-api=$DEADLOCK_API_ENABLED" \
    --service-enabled "deadlock-worker=$DEADLOCK_WORKER_ENABLED" \
    --service-enabled "deadlock-web=$DEADLOCK_WEB_ENABLED" \
    --timer-enabled-before "$CLOUDFLARE_TIMER_ENABLED" >/dev/null 2>/dev/null
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
  # A retained transaction is normally completed before this helper is
  # called, so the shared receipt may already be gone.  Absence is the
  # successful post-cleanup state, not a recovery failure.
  return 0
}

restart_recorded_services() {
  [[ "$SERVICE_STATE_CAPTURED" -eq 1 ]] || {
    public_status failed service_state >&2
    return 1
  }
  local service expected
  for service in deadlock-api deadlock-worker deadlock-web; do
    local enabled_expected
    case "$service" in
      deadlock-api) expected="$DEADLOCK_API_STATE"; enabled_expected="$DEADLOCK_API_ENABLED" ;;
      deadlock-worker) expected="$DEADLOCK_WORKER_STATE"; enabled_expected="$DEADLOCK_WORKER_ENABLED" ;;
      deadlock-web) expected="$DEADLOCK_WEB_STATE"; enabled_expected="$DEADLOCK_WEB_ENABLED" ;;
    esac
    if [[ "$SERVICE_ENABLEMENT_CAPTURED" -eq 1 ]]; then
      if [[ "$enabled_expected" == "enabled" ]]; then
        run_systemctl enable "$service" >/dev/null 2>/dev/null || return 1
      else
        run_systemctl disable "$service" >/dev/null 2>/dev/null || return 1
      fi
      [[ "$(read_unit_enabled "$service")" == "$enabled_expected" ]] || return 1
    fi
    if [[ "$expected" == "active" ]]; then
      run_systemctl restart "$service" >/dev/null 2>/dev/null || return 1
      require_unit_state "$service" active || return 1
    else
      run_systemctl stop "$service" >/dev/null 2>/dev/null || return 1
      require_unit_state "$service" inactive || return 1
    fi
  done
  if [[ "$SERVICE_ENABLEMENT_CAPTURED" -eq 1 ]]; then
    if [[ "$CLOUDFLARE_TIMER_ENABLED" == "enabled" ]]; then
      run_systemctl enable deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null || return 1
    else
      run_systemctl disable deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null || return 1
    fi
    [[ "$(read_unit_enabled deadlock-cloudflare-ips.timer)" == "$CLOUDFLARE_TIMER_ENABLED" ]] || return 1
  fi
  if [[ "$CLOUDFLARE_TIMER_STATE" == "active" ]]; then
    run_systemctl start deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null || return 1
    require_unit_state deadlock-cloudflare-ips.timer active || return 1
  else
    run_systemctl stop deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null || return 1
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
  restart_recorded_services || return 1
  verify_recorded_service_readiness || return 1
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
  if [[ "$INITIAL_INSTALL" -eq 1 ]]; then
    return 0
  fi
  run_systemctl stop deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null
  for attempt in {1..60}; do
    cloudflare_service_state="$(read_cloudflare_quiescence_state)"
    if [[ "$cloudflare_service_state" == "inactive" ]]; then
      break
    fi
    if [[ "$attempt" -eq 60 ]]; then
      public_status failed quiesce >&2
      return 1
    fi
    sleep 1
  done
  run_systemctl stop deadlock-api deadlock-worker deadlock-web >/dev/null 2>/dev/null
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
    # A clean first install has no prior release and therefore no safe old
    # helper/runtime/systemd authority. Recovery is limited to the durable
    # transaction and pointer topology; never infer or start host services.
    [[ ! -e "$APP_DIR/shared/.release-systemd-state.json" \
      && ! -L "$APP_DIR/shared/.release-systemd-state.json" ]] || {
      public_status failed recovery >&2
      return 1
    }
    return 0
  fi
  restore_previous_runtime "$original_current"
  restore_recorded_services
}

recover_failure_transaction() {
  local retained_phase="$1"
  case "$retained_phase" in
    prepared|venv-transitioned|snapshot-placed|current-switched|previous-switched|\
    pointers-switched|staged|recovery-authorized|recovery-restored|filesystem-restored-services-pending)
      ;;
    *)
      public_status failed recovery >&2
      return 1
      ;;
  esac

  local transaction_operation original_current original_previous
  transaction_operation="$(transaction_json | json_field operation)"
  original_current="$(transaction_json | json_field current_before)"
  original_previous="$(transaction_json | json_field previous_before)"
  # A first install has no prior service authority and must remain a
  # filesystem-only recovery.  Current-only and two-pointer installs must
  # carry the complete active/enabled snapshot before any restore/cleanup.
  if [[ -n "$original_current" ]]; then
    if quiesce_receipt_exists; then
      load_quiesce_service_state || return 1
    else
      load_recorded_service_state || return 1
    fi
  else
    /usr/bin/python3 -I "$TRANSACTION_TOOL" validate-service-snapshot \
      --state "$TRANSACTION_STATE" --require optional >/dev/null 2>/dev/null || return 1
  fi
  if [[ "$transaction_operation" == "install" && -n "$original_current" && -z "$original_previous" ]]; then
    if [[ "$retained_phase" != "recovery-restored" && "$retained_phase" != "filesystem-restored-services-pending" ]]; then
      /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
        --retain --service-pending --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
    fi
    if [[ "$(transaction_json | json_field phase)" == "filesystem-restored-services-pending" ]]; then
      restore_recorded_services || return 1
    /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
        --state "$TRANSACTION_STATE" \
        --expected filesystem-restored-services-pending \
        --phase recovery-restored >/dev/null 2>/dev/null || return 1
    fi
    /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
    clear_quiesce_receipt || return 1
    return 0
  fi
  if [[ "$retained_phase" != "recovery-restored" ]]; then
    /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
      --retain \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
  restore_snapshot_runtime "$(original_current_from_transaction)" || return 1
  /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
  if [[ "$retained_phase" != "recovery-restored" ]]; then
    /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
  fi
  clear_quiesce_receipt || return 1
}

recover_failure_uncertain_transaction() {
  if [[ "$INITIAL_INSTALL" -eq 1 ]]; then
    # A clean install has no prior runtime to restore. Its transaction-bound
    # systemd snapshot is the only authority, and it must be restored before
    # leaving the candidate/receipt retained for an explicit retry or abort.
    systemd_operation_reset || return 1
    local restore_status=0
    restore_initial_systemd_baseline || restore_status=$?
    # Recovery has stopped/disabled the units.  A post-activation marker would
    # make the next run require the state we just restored, so rewind the
    # durable phase before returning the failed deployment.
    if [[ "$restore_status" -eq 0 ]]; then
      rewind_initial_systemd_phase_for_retry || restore_status=$?
    fi
    systemd_operation_end
    return "$restore_status"
  fi
  if quiesce_receipt_exists; then
    load_quiesce_service_state || return 1
    verify_quiesce_receipt || return 1
    restore_snapshot_runtime "$(original_current_from_quiesce)" || return 1
    verify_quiesce_receipt || return 1
  else
    load_recorded_service_state || return 1
    /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
    restore_snapshot_runtime "$(original_current_from_transaction)" || return 1
    /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-original \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
  fi
  return 0
}

recover_failure_quiesce_only() {
  load_quiesce_service_state || return 1
  verify_quiesce_receipt || return 1
  restore_snapshot_runtime "$(original_current_from_quiesce)" || return 1
  verify_quiesce_receipt || return 1
  /usr/bin/python3 -I "$TRANSACTION_TOOL" abort-quiesce \
    --state "$QUIESCE_STATE" >/dev/null 2>/dev/null || return 1
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
    TRANSACTION_JSON="$(transaction_json)"
    set_initial_install_from_transaction
    if [[ "$INITIAL_INSTALL" -eq 1 && "$retained_phase" =~ ^(migration-pending|migration-failed|migration-applied|activation-pending|services-restarted|nginx-pending|nginx-applied|smoke-passed|systemd-activation-pending|systemd-activated|activation-committed|recovery-authorized)$ ]]; then
      recover_failure_uncertain_transaction
    elif [[ "$retained_phase" =~ ^(migration-pending|migration-failed|migration-applied)$ ]]; then
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
    systemd-activation-pending|systemd-activated|activation-committed|\
    recovery-authorized|recovery-restored|filesystem-restored-services-pending)
      ;;
    *)
      public_status failed recovery >&2
      return 1
      ;;
  esac
  local original_current original_previous phase_topology_ok candidate_from_receipt
  # Validate the immutable pre-migration snapshot before changing pointers or
  # the venv. An absent/invalid snapshot must not even enter filesystem
  # recovery, because there is no safe service state to restore afterward.
  TRANSACTION_JSON="$(transaction_json)"
  set_initial_install_from_transaction
  original_current="$(printf '%s' "$TRANSACTION_JSON" | json_field current_before)"
  original_previous="$(printf '%s' "$TRANSACTION_JSON" | json_field previous_before)"
  if [[ "$(printf '%s' "$TRANSACTION_JSON" | json_field operation)" == "install" \
    && -z "$original_previous" ]]; then
    # Install receipts without a previous release (including clean
    # first-install and current-only hosts) have no rollback target. Only the
    # transaction's own pointer/venv cleanup is authorized. Clean first
    # installs additionally restore the transaction-bound systemd baseline
    # before the candidate is removed.
    if [[ -n "$original_current" ]]; then
      phase_topology_ok=0
      case "$retained_phase" in
        prepared|venv-transitioned|snapshot-placed|staged|migration-pending|\
        migration-failed|migration-applied|recovery-restored)
          [[ -L "$APP_DIR/current" && "$(readlink -f "$APP_DIR/current")" == "$original_current" \
            && ! -e "$APP_DIR/previous" && ! -L "$APP_DIR/previous" ]] && phase_topology_ok=1
          ;;
        previous-switched)
          candidate_from_receipt="$(printf '%s' "$TRANSACTION_JSON" | json_field candidate_release)"
          if [[ -L "$APP_DIR/current" && "$(readlink -f "$APP_DIR/current")" == "$original_current" \
            && -L "$APP_DIR/previous" && "$(readlink -f "$APP_DIR/previous")" == "$original_current" ]]; then
            phase_topology_ok=1
          elif [[ -L "$APP_DIR/current" && "$(readlink -f "$APP_DIR/current")" == "$candidate_from_receipt" \
            && -L "$APP_DIR/previous" && "$(readlink -f "$APP_DIR/previous")" == "$original_current" ]]; then
            # The marker is persisted before the second pointer update. A
            # kill after that update but before current-switched is still an
            # exact transaction-owned topology and is recovered identically.
            phase_topology_ok=1
          fi
          ;;
        pointers-switched|current-switched)
          candidate_from_receipt="$(printf '%s' "$TRANSACTION_JSON" | json_field candidate_release)"
          [[ -L "$APP_DIR/current" && "$(readlink -f "$APP_DIR/current")" == "$candidate_from_receipt" \
            && -L "$APP_DIR/previous" && "$(readlink -f "$APP_DIR/previous")" == "$original_current" ]] && phase_topology_ok=1
          ;;
        filesystem-restored-services-pending)
          [[ -L "$APP_DIR/current" && "$(readlink -f "$APP_DIR/current")" == "$original_current" \
            && ! -e "$APP_DIR/previous" && ! -L "$APP_DIR/previous" ]] && phase_topology_ok=1
          ;;
      esac
      [[ "$phase_topology_ok" -eq 1 ]] || {
        public_status failed recovery >&2
        return 1
      }
    else
      phase_topology_ok=0
      case "$retained_phase" in
        prepared|venv-transitioned|snapshot-placed|staged|migration-pending|\
        migration-failed|migration-applied|recovery-restored)
          [[ ! -e "$APP_DIR/current" && ! -L "$APP_DIR/current" ]] && phase_topology_ok=1
          ;;
        pointers-switched|current-switched|activation-pending|services-restarted|\
        nginx-pending|nginx-applied|smoke-passed|systemd-activation-pending|\
        systemd-activated|activation-committed|recovery-authorized|\
        filesystem-restored-services-pending)
          candidate_from_receipt="$(printf '%s' "$TRANSACTION_JSON" | json_field candidate_release)"
          [[ -L "$APP_DIR/current" && "$(readlink -f "$APP_DIR/current")" == "$candidate_from_receipt" ]] && phase_topology_ok=1
          ;;
      esac
      [[ "$phase_topology_ok" -eq 1 ]] || {
        public_status failed recovery >&2
        return 1
      }
    fi
    if [[ -z "$original_current" || "$retained_phase" =~ ^(prepared|venv-transitioned|snapshot-placed|recovery-restored)$ ]]; then
      [[ ! -e "$APP_DIR/previous" && ! -L "$APP_DIR/previous" ]] || {
        public_status failed recovery >&2
        return 1
      }
    fi
    [[ ! -e "$APP_DIR/shared/.release-systemd-state.json" \
      && ! -L "$APP_DIR/shared/.release-systemd-state.json" ]] || {
      public_status failed recovery >&2
      return 1
    }
    if [[ -z "$original_current" ]]; then
      case "$retained_phase" in
        staged|recovery-restored)
          # These phases have no migration uncertainty, but a clean-install
          # receipt still owns the exact initial systemd topology. Prove it
          # immediately before candidate/receipt cleanup.
          verify_initial_systemd_with_fresh_deadline || return 1
          ;;
        migration-pending|migration-failed|migration-applied|activation-pending|\
        services-restarted|nginx-pending|nginx-applied|smoke-passed|\
        systemd-activation-pending|systemd-activated|activation-committed)
          # Restore and verify the receipt-bound baseline before writing the
          # recovery-authorized marker.  If this process dies here, the old
          # uncertain phase remains and the next abort retries restoration.
          restore_initial_systemd_with_fresh_deadline || return 1
          /usr/bin/python3 -I "$TRANSACTION_TOOL" authorize-recovery \
            --state "$TRANSACTION_STATE" \
            --confirm MIGRATION_NOT_REVERSED >/dev/null 2>/dev/null || return 1
          ;;
        recovery-authorized)
          # An earlier abort may have durably authorized recovery and then
          # died before filesystem cleanup.  Never skip the systemd proof.
          verify_initial_systemd_with_fresh_deadline || return 1
          ;;
      esac
    else
      case "$retained_phase" in
        migration-pending|migration-failed|migration-applied|activation-pending|\
        services-restarted|nginx-pending|nginx-applied|smoke-passed|\
        systemd-activation-pending|systemd-activated|activation-committed)
          public_status failed recovery >&2
          return 1
          ;;
      esac
    fi
    if [[ -n "$original_current" ]]; then
      load_recorded_service_state || return 1
      case "$retained_phase" in
        recovery-restored)
          ;;
        filesystem-restored-services-pending)
          ;;
        *)
          /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
            --retain --service-pending --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
          ;;
      esac
      if [[ "$(transaction_json | json_field phase)" == "filesystem-restored-services-pending" ]]; then
        restore_recorded_services || return 1
        /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
          --state "$TRANSACTION_STATE" \
          --expected filesystem-restored-services-pending \
          --phase recovery-restored >/dev/null 2>/dev/null || return 1
      fi
    else
      /usr/bin/python3 -I "$TRANSACTION_TOOL" validate-service-snapshot \
        --state "$TRANSACTION_STATE" --require optional >/dev/null 2>/dev/null || return 1
      /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
        --retain --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
    fi
    /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
      --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
    clear_quiesce_receipt || return 1
    public_status passed abort >&2
    return 0
  fi
  if [[ -z "$original_current" ]]; then
    original_current="$original_previous"
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
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1

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
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
  # Load the immutable snapshot before restoring the runtime.  The retained
  # transaction is the only authority for the exact enabled/active states;
  # do not let the abort path reach systemd with an empty in-memory snapshot.
  load_recorded_service_state || return 1
  restore_previous_runtime "$original_current" || return 1
  # The pointer check above protects the first recovery step; repeat it after
  # restoring units/Nginx so a concurrent pointer drift cannot authorize a
  # service start. The transaction tool also rechecks the recorded venv
  # identities at this boundary.
  /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
    --retain \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
  restore_recorded_services || return 1
  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
    --state "$TRANSACTION_STATE" >/dev/null 2>/dev/null || return 1
  clear_quiesce_receipt || return 1
  public_status passed abort >&2
}

abort_quiesce_receipt() {
  load_quiesce_service_state || return 1
  verify_quiesce_receipt || return 1
  restore_snapshot_runtime "$(original_current_from_quiesce)" || return 1
  verify_quiesce_receipt || return 1
  /usr/bin/python3 -I "$TRANSACTION_TOOL" abort-quiesce \
    --state "$QUIESCE_STATE" >/dev/null 2>/dev/null || return 1
  public_status passed abort >&2
}

acquire_release_lock

run_release_preflight_quiet() {
  release_preflight "$@"
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
  run_release_preflight_quiet --defer-active-alembic-revision-check \
    >/dev/null 2>/dev/null
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
# The live current pointer may already point at the candidate after a crash
# between promotion and activation. Re-derive the immutable topology from the
# transaction rather than from that mutable pointer.
set_initial_install_from_transaction
if [[ "$INITIAL_INSTALL" -eq 1 && "$(printf '%s' "$TRANSACTION_JSON" | json_field phase)" == "staged" ]]; then
  capture_initial_systemd_baseline || {
    public_status failed systemd >&2
    exit 1
  }
elif [[ "$INITIAL_INSTALL" -eq 1 ]]; then
  validate_initial_systemd_receipt || {
    public_status failed systemd >&2
    exit 1
  }
fi
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
  release_preflight --defer-active-alembic-revision-check \
    >/dev/null 2>/dev/null
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
  migration-applied|previous-switched|current-switched|pointers-switched|\
  activation-pending|services-restarted|nginx-pending|nginx-applied|\
  smoke-passed|systemd-activation-pending|systemd-activated|activation-committed)
    ;;
  *)
    public_status failed transaction >&2
    exit 1
    ;;
esac

phase="$(transaction_phase)"
case "$phase" in
  migration-applied)
    if [[ -n "$CURRENT_BEFORE" ]]; then
      /usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
        --state "$TRANSACTION_STATE" --name previous --target "$CURRENT_BEFORE" \
        >/dev/null 2>/dev/null || exit 1
      # This marker is written between the two independent pointer updates.
      # A retry can therefore distinguish a previous-only move from a fully
      # promoted pair without guessing from the live filesystem.
      set_phase migration-applied previous-switched || exit 1
      phase=previous-switched
    else
      /usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
        --state "$TRANSACTION_STATE" --name current --target "$CANDIDATE" \
        >/dev/null 2>/dev/null || exit 1
      set_phase migration-applied current-switched || exit 1
      phase=current-switched
    fi
    ;;
esac
case "$phase" in
  previous-switched)
    /usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
      --state "$TRANSACTION_STATE" --name current --target "$CANDIDATE" \
      >/dev/null 2>/dev/null || exit 1
    set_phase previous-switched current-switched || exit 1
    phase=current-switched
    ;;
esac
case "$phase" in
  current-switched)
    set_phase current-switched pointers-switched || exit 1
    phase=pointers-switched
    ;;
esac
case "$phase" in
  pointers-switched)
    set_phase pointers-switched activation-pending || exit 1
    phase=activation-pending
    ;;
esac

if [[ "$phase" == "activation-pending" ]]; then
  # The trusted live-QA payload is part of activation identity. Reconcile it
  # while the canonical release lock is still held, before any post-activation
  # readiness or smoke work can observe the new current release.
  if [[ "$INITIAL_INSTALL" -eq 1 ]]; then
    systemd_operation_reset || {
      public_status failed systemd >&2
      exit 1
    }
  fi
  LIVE_QA_RUNTIME_INSTALLER="$CANDIDATE/tools/platform_live_qa_runtime_install.py"
  if [[ ! -f "$LIVE_QA_RUNTIME_INSTALLER" || -L "$LIVE_QA_RUNTIME_INSTALLER" ]]; then
    public_status failed liveqa_runtime >&2
    exit 1
  fi
  run_live_qa_reconcile || {
    public_status failed liveqa_runtime >&2
    exit 1
  }
  # Unit files are prepared without mutating enablement or activation. A
  # clean first install performs its persistent activation only after smoke
  # has passed and the durable systemd-activation-pending phase is recorded.
  if [[ "$INITIAL_INSTALL" -eq 1 ]]; then
    PLATFORM_ENABLE_SYSTEMD_UNITS=0 run_initial_systemd_candidate \
      tools/platform_install_systemd_units.sh
  else
    PLATFORM_ENABLE_SYSTEMD_UNITS=0 run_candidate tools/platform_install_systemd_units.sh
  fi
  run_systemctl restart deadlock-api deadlock-worker deadlock-web \
    >/dev/null 2>/dev/null
  for service in deadlock-api deadlock-worker deadlock-web; do
    require_unit_state "$service" active || {
      public_status failed service_state >&2
      exit 1
    }
  done
  wait_for_activation_readiness || {
    public_status failed readiness >&2
    exit 1
  }
  if [[ "$CLOUDFLARE_TIMER_STATE" == "active" ]]; then
    run_systemctl start deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null
    require_unit_state deadlock-cloudflare-ips.timer active || {
      public_status failed service_state >&2
      exit 1
    }
  fi
  WRITERS_QUIESCED=0
  set_phase activation-pending services-restarted
  if [[ "$INITIAL_INSTALL" -eq 1 ]]; then
    systemd_operation_end
  fi
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
  if [[ "$INITIAL_INSTALL" -eq 1 ]]; then
    set_phase smoke-passed systemd-activation-pending
    phase=systemd-activation-pending
  else
    set_phase smoke-passed activation-committed
    phase=activation-committed
  fi
fi

if [[ "$phase" == "systemd-activation-pending" ]]; then
  # The receipt was captured and fsynced before migration/pointer activation.
  # Return to that exact baseline before enabling anything so a retry after a
  # partial activation is deterministic and never accumulates enablement.
  systemd_operation_reset || {
    public_status failed systemd >&2
    exit 1
  }
  restore_initial_systemd_baseline || {
    public_status failed systemd >&2
    exit 1
  }
  PLATFORM_ENABLE_SYSTEMD_UNITS=1 run_initial_systemd_candidate \
    tools/platform_install_systemd_units.sh
  run_systemctl restart deadlock-api deadlock-worker deadlock-web \
    >/dev/null 2>/dev/null || {
    public_status failed service_state >&2
    exit 1
  }
  for service in deadlock-api deadlock-worker deadlock-web; do
    require_unit_state "$service" active || {
      public_status failed service_state >&2
      exit 1
    }
    require_unit_enabled "$service" || {
      public_status failed service_state >&2
      exit 1
    }
  done
  for timer in \
    deadlock-maintenance.timer \
    deadlock-logrotate.timer \
    deadlock-cloudflare-ips.timer \
    deadlock-health-monitor.timer; do
    require_unit_enabled "$timer" || {
      public_status failed service_state >&2
      exit 1
    }
    require_unit_state "$timer" active || {
      public_status failed service_state >&2
      exit 1
    }
  done
  wait_for_activation_readiness || {
    public_status failed readiness >&2
    exit 1
  }
  set_phase systemd-activation-pending systemd-activated
  systemd_operation_end
  phase=systemd-activated
fi

if [[ "$phase" == "systemd-activated" ]]; then
  systemd_operation_reset || {
    public_status failed systemd >&2
    exit 1
  }
  verify_initial_systemd_activation || {
    public_status failed systemd >&2
    exit 1
  }
  systemd_operation_end
  set_phase systemd-activated activation-committed
  phase=activation-committed
fi

if [[ "$phase" == "activation-committed" ]]; then
  systemd_operation_reset || {
    public_status failed systemd >&2
    exit 1
  }
  verify_initial_systemd_activation || {
    public_status failed systemd >&2
    exit 1
  }
  systemd_operation_end
  clear_quiesce_receipt
  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"

fi

platform_release_lock_close
trap - EXIT
public_status passed deployment
