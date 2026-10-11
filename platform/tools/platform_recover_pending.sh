#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

ORIGINAL_ARGS=("$@")
APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
SYSTEMCTL_BIN="/usr/bin/systemctl"
SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"
NGINX_BIN="/usr/sbin/nginx"
NGINX_TIMEOUT_BIN="/usr/bin/timeout"
NGINX_CONFIG_TIMEOUT_SECONDS=30
PUBLIC_RELEASE_SLUG="unavailable"
PUBLIC_SOURCE_SHA="unavailable"

public_status() {
  local status="$1" class="${2:-recovery}"
  printf 'RELEASE_RUNTIME schema=1 status=%s class=%s release_slug=%s source_sha=%s\n' \
    "$status" "$class" "$PUBLIC_RELEASE_SLUG" "$PUBLIC_SOURCE_SHA"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      APP_DIR="$2"; shift 2
      ;;
    --systemctl)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      SYSTEMCTL_BIN="$2"; shift 2
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_recover_pending.sh --app-dir <path> [--systemctl <absolute-path>]
EOF
      exit 0
      ;;
    *) public_status failed argument >&2; exit 1 ;;
  esac
done

[[ "$EUID" -eq 0 ]] || { public_status failed privilege >&2; exit 1; }
[[ "$APP_DIR" == /* && "$APP_DIR" != "/" ]] || { public_status failed argument >&2; exit 1; }
[[ "$SYSTEMCTL_BIN" == /* && "$SYSTEMCTL_BIN" != *$'\n'* ]] || {
  public_status failed argument >&2
  exit 1
}

# The immutable wrapper still performs final active-state checks directly so
# that a receipt cannot be cleared on an unverified runtime.  Bound those
# checks with the same trusted timeout policy as every other systemctl call.
run_systemctl() {
  "$SYSTEMCTL_TIMEOUT_BIN" --signal=TERM --kill-after=5s 30s "$SYSTEMCTL_BIN" "$@"
}

json_field() {
  local field="$1"
  /usr/bin/python3 -I -c \
    'import json,sys; value=json.load(sys.stdin)[sys.argv[1]]; print("" if value is None else value)' \
    "$field" 2>/dev/null
}

run_nginx_config_test() {
  "$NGINX_TIMEOUT_BIN" --signal=TERM --kill-after=5s "${NGINX_CONFIG_TIMEOUT_SECONDS}s" \
    "$NGINX_BIN" -t >/dev/null 2>/dev/null
}

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
GENERATION_SHA="$(basename "$TOOLS_DIR")"
[[ "$GENERATION_SHA" =~ ^[0-9a-f]{64}$ ]] || { public_status failed generation >&2; exit 1; }

validate_generation_helper() {
  local mode="$2" path="$TOOLS_DIR/$1"
  test -f "$path" && test ! -L "$path" \
    && test "$(stat -c '%u:%h:%a' "$path" 2>/dev/null)" = "0:1:$mode" \
    || { public_status failed generation >&2; exit 1; }
}

validate_generation_helper platform_recovery_bootstrap.py 444
validate_generation_helper platform_release_lock.sh 555
validate_generation_helper platform_release_restore_runtime.sh 555
validate_generation_helper platform_release_systemd_state.py 444
validate_generation_helper platform_release_transaction.py 444
/usr/bin/python3 -I -B "$TOOLS_DIR/platform_recovery_bootstrap.py" \
  validate-generation --generation "$TOOLS_DIR" --bundle-sha "$GENERATION_SHA" \
  --capability recover_pending \
  >/dev/null 2>/dev/null \
  || { public_status failed generation >&2; exit 1; }

# This immutable wrapper owns lock/transaction/systemd/restore orchestration.
# It is intentionally separate from the abort-retained-only entrypoint.
# shellcheck source=/dev/null
source "$TOOLS_DIR/platform_release_lock.sh"
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
platform_release_lock_open || { public_status failed lock >&2; exit 3; }
trap platform_release_lock_close EXIT

STATE="$APP_DIR/shared/.release-operation.json"
QUIESCE_STATE="$APP_DIR/shared/.release-quiesce.json"
SYSTEMD_STATE="$APP_DIR/shared/.release-systemd-state.json"
TRANSACTION_TOOL="$TOOLS_DIR/platform_release_transaction.py"
SYSTEMD_STATE_TOOL="$TOOLS_DIR/platform_release_systemd_state.py"
RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"
test -f "$STATE" && test ! -L "$STATE" \
  && test "$(stat -c '%u:%h:%a' "$STATE" 2>/dev/null)" = "0:1:600" \
  || { public_status failed transaction >&2; exit 1; }

pending_operation="$({
  /usr/bin/python3 -I - "$STATE" <<'PY'
import json
import sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
if record.get("operation") not in {"install", "rollback"}:
    raise SystemExit(1)
print(record["operation"])
PY
} 2>/dev/null)" || { public_status failed transaction >&2; exit 1; }
transaction_context="$({
  /usr/bin/python3 -I - "$STATE" <<'PY'
import json
import sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
for key in ("operation_id", "phase", "current_before", "previous_before"):
    value = record.get(key)
    print("" if value is None else value)
PY
} 2>/dev/null)" || { public_status failed transaction >&2; exit 1; }
mapfile -t transaction_fields <<<"$transaction_context"
operation_id="${transaction_fields[0]:-}"
transaction_phase="${transaction_fields[1]:-}"
original_current="${transaction_fields[2]:-}"
original_previous="${transaction_fields[3]:-}"

[[ "$operation_id" =~ ^[0-9a-f]{32}$ ]] || {
  if [[ -z "$operation_id" && "$pending_operation" == "install" && "$transaction_phase" == "quiesce-pending" ]]; then
    # This is the narrow pre-promotion receipt written by the immutable
    # transaction helper.  It has no operation id by design, but its exact
    # schema/pointers/snapshot must be verified before restoring any unit.
    # A full operation or systemd receipt alongside it is an ambiguous state.
    [[ ! -e "$QUIESCE_STATE" && ! -L "$QUIESCE_STATE" ]] \
      || { public_status failed transaction >&2; exit 1; }
    [[ ! -e "$SYSTEMD_STATE" && ! -L "$SYSTEMD_STATE" ]] \
      || { public_status failed systemd_state >&2; exit 1; }
    /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-quiesce --state "$STATE" \
      >/dev/null 2>/dev/null \
      || { public_status failed transaction >&2; exit 1; }
    if [[ -z "$original_current" ]]; then
      # A first-install pre-promotion receipt is a compatibility no-op only
      # when its complete snapshot proves every unit inactive/disabled.  The
      # immutable validator fails closed for partial/active snapshots; no
      # systemctl command is issued on this topology.
      /usr/bin/python3 -I "$TRANSACTION_TOOL" validate-quiesce-noop \
        --state "$STATE" >/dev/null 2>/dev/null \
        || { public_status failed service_state >&2; exit 1; }
    else
      /usr/bin/python3 -I "$TRANSACTION_TOOL" restore-quiesce \
        --state "$STATE" --systemctl "$SYSTEMCTL_BIN" \
        >/dev/null 2>/dev/null \
        || { public_status failed service_state >&2; exit 1; }
    fi
    /usr/bin/python3 -I "$TRANSACTION_TOOL" abort-quiesce --state "$STATE" \
      >/dev/null 2>/dev/null \
      || { public_status failed transaction >&2; exit 1; }
    test ! -e "$STATE" && test ! -L "$STATE" \
      || { public_status failed transaction >&2; exit 1; }
    public_status passed recovery
    exit 0
  fi
  public_status failed transaction >&2
  exit 1
}

if [[ "$pending_operation" == "rollback" ]]; then
  # Rollback recovery is deliberately implemented in this immutable wrapper.
  # The old release and its tools are data-plane inputs only; no retained
  # current/tools control helper may be selected for any rollback phase.
  [[ -n "$original_current" && -n "$original_previous" ]] || {
    public_status failed topology >&2
    exit 1
  }
  # A crash after the receipt has been durably cleared must not recreate or
  # query systemd.  The transaction phase is the immutable two-phase commit
  # marker; finish only the already-proven filesystem cleanup on retry.
  if [[ ! -e "$SYSTEMD_STATE" && ! -L "$SYSTEMD_STATE" ]]; then
    case "$transaction_phase" in
      prepared|venv-transitioned|snapshot-placed|current-switched|previous-switched|pointers-switched)
        # Rollback captures the systemd receipt after the pointer transaction
        # reaches its durable boundary.  An early crash can therefore leave
        # no receipt at all; restore only the immutable filesystem transaction
        # and never synthesize or query live systemd state.
        /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --retain --state "$STATE" \
          >/dev/null 2>/dev/null \
          || { public_status failed transaction >&2; exit 1; }
        /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery --state "$STATE" \
          >/dev/null 2>/dev/null \
          || { public_status failed transaction >&2; exit 1; }
        test ! -e "$STATE" && test ! -L "$STATE" \
          || { public_status failed transaction >&2; exit 1; }
        public_status passed recovery
        exit 0
        ;;
      recovery-restored)
        /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery --state "$STATE" \
          >/dev/null 2>/dev/null \
          || { public_status failed transaction >&2; exit 1; }
        test ! -e "$STATE" && test ! -L "$STATE" \
          || { public_status failed transaction >&2; exit 1; }
        public_status passed recovery
        exit 0
        ;;
      rollback-runtime-applied)
        /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$STATE" \
          >/dev/null 2>/dev/null \
          || { public_status failed transaction >&2; exit 1; }
        test ! -e "$STATE" && test ! -L "$STATE" \
          || { public_status failed transaction >&2; exit 1; }
        public_status passed recovery
        exit 0
        ;;
    esac
  fi
  test -f "$SYSTEMD_STATE" && test ! -L "$SYSTEMD_STATE" \
    && test "$(stat -c '%u:%g:%h:%a' "$SYSTEMD_STATE" 2>/dev/null)" = "0:0:1:600" \
    || { public_status failed systemd_state >&2; exit 1; }

  rollback_systemd_validate() {
    local helper_release="$1"
    /usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" validate \
      --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
      --transaction "$STATE" --helper-release "$helper_release" \
      --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null
  }

  rollback_systemd_restore() {
    local active="$1" helper_release="$2" command=(restore-enabled)
    if [[ "$active" == "1" ]]; then
      command=(restore)
    elif [[ "$active" != "0" ]]; then
      return 1
    fi
    /usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" "${command[@]}" \
      --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
      --transaction "$STATE" --helper-release "$helper_release" \
      --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null
  }

  rollback_systemd_verify() {
    local helper_release="$1"
    /usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" verify \
      --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
      --transaction "$STATE" --helper-release "$helper_release" \
      --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null
  }

  rollback_systemd_clear() {
    local helper_release="$1"
    /usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" clear \
      --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
      --transaction "$STATE" --helper-release "$helper_release" \
      --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null
  }

  # Validate the operation/receipt pair before any phase action.  This checks
  # the operation id, release identities and every recorded helper digest while
  # the receipt still exists; the validate command never queries systemd.
  rollback_systemd_validate "$original_current" \
    || { public_status failed systemd_state >&2; exit 1; }

  case "$transaction_phase" in
    prepared|venv-transitioned|snapshot-placed|current-switched|previous-switched|pointers-switched)
      # Before the runtime boundary, restore only the durable filesystem
      # transaction.  The paired receipt is cleared only after recovery has
      # reached recovery-restored and is still bound to the same operation.
      /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --retain --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      rollback_systemd_validate "$original_current" \
        || { public_status failed systemd_state >&2; exit 1; }
      rollback_systemd_clear "$original_current" \
        || { public_status failed systemd_state >&2; exit 1; }
      test ! -e "$SYSTEMD_STATE" && test ! -L "$SYSTEMD_STATE" \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      ;;
    rollback-runtime-pending|services-restarted|smoke-passed|filesystem-restored-runtime-pending)
      # Runtime may have been applied to the rollback target.  First restore
      # the original pointers/venv, then restore the original release's
      # release-specific files without restart, and finally restore/verify the
      # captured active systemd state through the immutable helper.
      if [[ "$transaction_phase" != "filesystem-restored-runtime-pending" ]]; then
        /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --retain --runtime-pending --state "$STATE" \
          >/dev/null 2>/dev/null \
          || { public_status failed transaction >&2; exit 1; }
      fi
      rollback_systemd_validate "$original_current" \
        || { public_status failed systemd_state >&2; exit 1; }
      PLATFORM_ENABLE_SYSTEMD_UNITS=0 "$RESTORE_TOOL" \
        --app-dir "$APP_DIR" --release "$original_current" --no-restart \
        --systemd-state "$SYSTEMD_STATE" --transaction "$STATE" \
        --live-qa-runtime-installer "$original_current/tools/platform_live_qa_runtime_install.py" \
        --systemctl "$SYSTEMCTL_BIN" --expected-csp-mode enforce \
        --edge-origin https://127.0.0.1 --edge-host old-sparky.com \
        --public-edge-origin https://old-sparky.com \
        || { public_status failed runtime >&2; exit 1; }
      rollback_systemd_restore 1 "$original_current" \
        || { public_status failed systemd_state >&2; exit 1; }
      rollback_systemd_verify "$original_current" \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
        --state "$STATE" \
        --expected filesystem-restored-runtime-pending \
        --phase recovery-restored \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
        --retain-receipt --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      rollback_systemd_clear "$original_current" \
        || { public_status failed systemd_state >&2; exit 1; }
      test ! -e "$SYSTEMD_STATE" && test ! -L "$SYSTEMD_STATE" \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      ;;
    rollback-cache-purged)
      # Filesystem rollback is complete and the target pointer pair must stay
      # swapped.  Resume only the target runtime/systemd phase after the
      # transaction proves the pre-activation v1 cache purge completed, then
      # mark the durable runtime-applied boundary before receipt cleanup.
      rollback_systemd_validate "$original_previous" \
        || { public_status failed systemd_state >&2; exit 1; }
      pending_release="$(readlink -f "$APP_DIR/current" 2>/dev/null || true)"
      [[ "$pending_release" == "$original_previous" ]] \
        || { public_status failed topology >&2; exit 1; }
      runtime_reconcile_args=()
      runtime_installer="$pending_release/tools/platform_live_qa_runtime_install.py"
      # A missing or malformed manifest cannot opt into the legacy bridge.
      # Keep the ordinary target installer as the strict default; the restore
      # path validates the selected immutable release before accepting it.
      pending_source_sha="$(/usr/bin/cat "$pending_release/RELEASE.json" | json_field source_git_commit || true)"
      if [[ "$pending_release" == "$original_previous" \
        && "$pending_source_sha" == "6343099bb7686671bdef49d0c4ecd10f21ef19d2" ]]; then
        # Reconcile the exact M8 target with the transaction-bound current
        # release's compatible installer. The legacy target's own installer
        # cannot accept the narrowly scoped reconcile option.
        runtime_installer="$original_current/tools/platform_live_qa_runtime_install.py"
        runtime_reconcile_args=(--rollback-reconcile-transaction "$STATE")
      fi
      PLATFORM_ENABLE_SYSTEMD_UNITS=0 "$RESTORE_TOOL" \
        --app-dir "$APP_DIR" --release "$pending_release" --no-restart \
        --systemd-state "$SYSTEMD_STATE" --transaction "$STATE" \
        --live-qa-runtime-installer "$runtime_installer" \
        "${runtime_reconcile_args[@]}" \
        --systemctl "$SYSTEMCTL_BIN" --expected-csp-mode enforce \
        --edge-origin https://127.0.0.1 --edge-host old-sparky.com \
        --public-edge-origin https://old-sparky.com \
        || { public_status failed runtime >&2; exit 1; }
      rollback_systemd_restore 1 "$original_previous" \
        || { public_status failed systemd_state >&2; exit 1; }
      rollback_systemd_verify "$original_previous" \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
        --state "$STATE" --expected rollback-cache-purged \
        --phase rollback-runtime-applied \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      rollback_systemd_clear "$original_previous" \
        || { public_status failed systemd_state >&2; exit 1; }
      test ! -e "$SYSTEMD_STATE" && test ! -L "$SYSTEMD_STATE" \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      ;;
    restart-pending)
      # Older receipts have no durable proof that the legacy profile cache was
      # purged.  Keep the stopped state and both receipts for operator recovery.
      public_status failed cache_purge >&2
      exit 1
      ;;
    rollback-runtime-applied)
      # No runtime replay on a retry after the two-phase runtime boundary.
      rollback_systemd_validate "$original_previous" \
        || { public_status failed systemd_state >&2; exit 1; }
      rollback_systemd_verify "$original_previous" \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete \
        --retain-receipt --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      rollback_systemd_clear "$original_previous" \
        || { public_status failed systemd_state >&2; exit 1; }
      test ! -e "$SYSTEMD_STATE" && test ! -L "$SYSTEMD_STATE" \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      ;;
    recovery-restored)
      # Idempotent finish after recover --retain.  Do not rerun runtime or
      # systemd actions; only prove and consume the same operation pair.
      rollback_systemd_validate "$original_current" \
        || { public_status failed systemd_state >&2; exit 1; }
      rollback_systemd_verify "$original_current" \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
        --retain-receipt --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      rollback_systemd_clear "$original_current" \
        || { public_status failed systemd_state >&2; exit 1; }
      test ! -e "$SYSTEMD_STATE" && test ! -L "$SYSTEMD_STATE" \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      ;;
    *)
      public_status failed transaction >&2
      exit 1
      ;;
  esac
  test ! -e "$STATE" && test ! -L "$STATE" \
    || { public_status failed transaction >&2; exit 1; }
  public_status passed recovery
  exit 0
fi

if [[ -z "$original_previous" ]]; then
  [[ ! -e "$SYSTEMD_STATE" && ! -L "$SYSTEMD_STATE" ]] \
    || { public_status failed topology >&2; exit 1; }
  # Validate the live pair against the durable phase before any pointer or
  # service cleanup.  Current-only promotion temporarily uses ``previous``
  # for the old current release, and a kill after the second symlink can
  # leave the post-promotion pair while the marker still says
  # previous-switched.  The immutable transaction helper owns that exact
  # closed topology matrix; do not guess from nullable original pointers.
  /usr/bin/python3 -I "$TRANSACTION_TOOL" validate-recovery-pointers \
    --state "$STATE" >/dev/null 2>/dev/null \
    || { public_status failed topology >&2; exit 1; }
  if [[ -n "$original_current" ]]; then
    # A current-only upgrade has a complete snapshot but no first-install
    # systemd receipt.  Restore it through the same durable retry boundary as
    # an upgrade, while retaining the transaction until services are verified.
    /usr/bin/python3 -I "$TRANSACTION_TOOL" validate-service-snapshot \
      --state "$STATE" --require present \
      >/dev/null 2>/dev/null \
      || { public_status failed service_state >&2; exit 1; }
    case "$transaction_phase" in
      filesystem-restored-services-pending)
        ;;
      recovery-restored)
        /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
          --state "$STATE" >/dev/null 2>/dev/null \
          || { public_status failed transaction >&2; exit 1; }
        test ! -e "$STATE" && test ! -L "$STATE" \
          || { public_status failed transaction >&2; exit 1; }
        public_status passed recovery
        exit 0
        ;;
      prepared|venv-transitioned|snapshot-placed|current-switched|previous-switched|pointers-switched|staged|recovery-authorized)
        /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
          --retain --service-pending --state "$STATE" \
          >/dev/null 2>/dev/null \
          || { public_status failed transaction >&2; exit 1; }
        ;;
      *)
        public_status failed transaction >&2
        exit 1
        ;;
    esac
    /usr/bin/python3 -I "$TRANSACTION_TOOL" restore-services \
      --state "$STATE" --systemctl "$SYSTEMCTL_BIN" \
      >/dev/null 2>/dev/null \
      || { public_status failed service_state >&2; exit 1; }
    /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
      --state "$STATE" --expected filesystem-restored-services-pending \
      --phase recovery-restored >/dev/null 2>/dev/null \
      || { public_status failed transaction >&2; exit 1; }
    /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
      --state "$STATE" >/dev/null 2>/dev/null \
      || { public_status failed transaction >&2; exit 1; }
    test ! -e "$STATE" && test ! -L "$STATE" \
      || { public_status failed transaction >&2; exit 1; }
    public_status passed recovery
    exit 0
  fi
  initial_systemd_authority=0
  if [[ -z "$original_current" ]]; then
    case "$transaction_phase" in
      staged|migration-pending|migration-failed|migration-applied|activation-pending|\
      services-restarted|nginx-pending|nginx-applied|smoke-passed|\
      systemd-activation-pending|systemd-activated|activation-committed|\
      recovery-authorized|recovery-restored)
        initial_systemd_authority=1
        ;;
    esac
    if [[ "$initial_systemd_authority" -eq 1 ]]; then
      # Initial-install recovery uses the same durable transaction receipt as
      # deploy/abort. Restore and verify systemd before authorizing migration
      # recovery or allowing any candidate/receipt cleanup.
      /usr/bin/python3 -I "$TRANSACTION_TOOL" validate-initial-systemd \
        --state "$STATE" --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
        || { public_status failed systemd_state >&2; exit 1; }
      if [[ "$transaction_phase" != "recovery-restored" ]]; then
        /usr/bin/python3 -I "$TRANSACTION_TOOL" restore-initial-systemd \
          --state "$STATE" --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
          || { public_status failed systemd_state >&2; exit 1; }
      fi
      case "$transaction_phase" in
        migration-pending|migration-failed|migration-applied|activation-pending|\
        services-restarted|nginx-pending|nginx-applied|smoke-passed|\
        systemd-activation-pending|systemd-activated|activation-committed)
          /usr/bin/python3 -I "$TRANSACTION_TOOL" authorize-recovery \
            --state "$STATE" --confirm MIGRATION_NOT_REVERSED \
            >/dev/null 2>/dev/null \
            || { public_status failed transaction >&2; exit 1; }
          ;;
      esac
      transaction_phase="$(/usr/bin/python3 -I "$TRANSACTION_TOOL" status \
        --state "$STATE" --json 2>/dev/null | json_field phase)" \
        || { public_status failed transaction >&2; exit 1; }
    fi
    /usr/bin/python3 -I "$TRANSACTION_TOOL" validate-service-snapshot \
      --state "$STATE" --require optional \
      >/dev/null 2>/dev/null \
      || { public_status failed service_state >&2; exit 1; }
    if [[ "$initial_systemd_authority" -eq 1 ]]; then
      if [[ "$transaction_phase" != "recovery-restored" ]]; then
        /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --retain --state "$STATE" \
          >/dev/null 2>/dev/null \
          || { public_status failed transaction >&2; exit 1; }
      fi
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
        --retain-receipt --state "$STATE" >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" verify-initial-systemd \
        --state "$STATE" --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
        || { public_status failed systemd_state >&2; exit 1; }
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery \
        --state "$STATE" >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
    else
      /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
    fi
    test ! -e "$STATE" && test ! -L "$STATE" \
      || { public_status failed transaction >&2; exit 1; }
    public_status passed
    exit 0
  fi

  /usr/bin/python3 -I "$TRANSACTION_TOOL" validate-service-snapshot \
    --state "$STATE" --require present \
    >/dev/null 2>/dev/null \
    || { public_status failed service_state >&2; exit 1; }
  case "$transaction_phase" in
    filesystem-restored-services-pending)
      ;;
    recovery-restored)
      /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      test ! -e "$STATE" && test ! -L "$STATE" \
        || { public_status failed transaction >&2; exit 1; }
      public_status passed
      exit 0
      ;;
    prepared|venv-transitioned|snapshot-placed|current-switched|previous-switched|pointers-switched|staged|recovery-authorized)
      /usr/bin/python3 -I "$TRANSACTION_TOOL" recover \
        --retain --service-pending --state "$STATE" \
        >/dev/null 2>/dev/null \
        || { public_status failed transaction >&2; exit 1; }
      ;;
    *)
      public_status failed transaction >&2
      exit 1
      ;;
  esac
  /usr/bin/python3 -I "$TRANSACTION_TOOL" restore-services \
    --state "$STATE" --systemctl "$SYSTEMCTL_BIN" \
    >/dev/null 2>/dev/null \
    || { public_status failed service_state >&2; exit 1; }
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$STATE" \
    --expected filesystem-restored-services-pending \
    --phase recovery-restored \
    >/dev/null 2>/dev/null \
    || { public_status failed transaction >&2; exit 1; }
  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery --state "$STATE" \
    >/dev/null 2>/dev/null \
    || { public_status failed transaction >&2; exit 1; }
  test ! -e "$STATE" && test ! -L "$STATE" \
    || { public_status failed transaction >&2; exit 1; }
  public_status passed
  exit 0
fi

[[ -n "$original_current" ]] && [[ -n "$original_previous" ]] \
  && [[ "$pending_operation" == "install" ]] || {
  public_status failed topology >&2
  exit 1
}

complete_service_snapshot() {
  /usr/bin/python3 -I - "$STATE" <<'PY'
import json
import sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
service_state = record.get("service_state_before")
service_enabled = record.get("service_enabled_before")
expected = {"deadlock-api", "deadlock-worker", "deadlock-web"}
if (
    not isinstance(service_state, dict)
    or set(service_state) != expected
    or any(value not in {"active", "inactive"} for value in service_state.values())
    or not isinstance(service_enabled, dict)
    or set(service_enabled) != expected
    or any(value not in {"enabled", "disabled"} for value in service_enabled.values())
    or record.get("quiesced_services") != ["deadlock-api", "deadlock-worker", "deadlock-web"]
    or type(record.get("timer_active_before")) is not bool
    or record.get("timer_enabled_before") not in {"enabled", "disabled"}
):
    raise SystemExit(1)
PY
}

if [[ -e "$SYSTEMD_STATE" || -L "$SYSTEMD_STATE" ]]; then
  test -f "$SYSTEMD_STATE" && test ! -L "$SYSTEMD_STATE" \
    && test "$(stat -c '%u:%h:%a' "$SYSTEMD_STATE" 2>/dev/null)" = "0:1:600" \
    || { public_status failed systemd_state >&2; exit 1; }
  /usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" validate \
    --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
    --transaction "$STATE" --helper-release "$original_current" \
    --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
    || { public_status failed systemd_state >&2; exit 1; }
else
  # Reject null/partial snapshots before capture can query systemctl or write
  # the durable receipt; the operation receipt remains retained on failure.
  complete_service_snapshot \
    || { public_status failed service_state >&2; exit 1; }
  /usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" capture-transaction \
    --state "$SYSTEMD_STATE" --transaction "$STATE" --app-dir "$APP_DIR" \
    --helper-release "$original_current" --require-helper-manifest \
    --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
    || { public_status failed systemd_state >&2; exit 1; }
fi

/usr/bin/python3 -I "$TRANSACTION_TOOL" recover --retain --state "$STATE" \
  >/dev/null 2>/dev/null \
  || { public_status failed transaction >&2; exit 1; }
test -f "$STATE" && test ! -L "$STATE" \
  || { public_status failed transaction >&2; exit 1; }

current_release="$(readlink -f "$APP_DIR/current" 2>/dev/null || true)"
test -L "$APP_DIR/current" && test -n "$current_release" \
  && test -d "$current_release" && test ! -L "$current_release" \
  && test "$(dirname "$current_release")" = "$APP_DIR/releases" \
  && test "$(stat -c '%u:%a' "$current_release" 2>/dev/null)" = "0:755" \
  && test "$(stat -c '%h' "$current_release" 2>/dev/null)" -ge 2 \
  || { public_status failed layout >&2; exit 1; }
expected_current="$({
  /usr/bin/python3 -I - "$SYSTEMD_STATE" <<'PY'
import json
import sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
value = record.get("current_before")
if not isinstance(value, str):
    raise SystemExit(1)
print(value)
PY
} 2>/dev/null)" || { public_status failed systemd_state >&2; exit 1; }
test "$current_release" = "$expected_current" \
  || { public_status failed topology >&2; exit 1; }
PUBLIC_RELEASE_SLUG="$(basename "$current_release")"
source_sha="$({
  /usr/bin/python3 -I - "$current_release/RELEASE.json" <<'PY'
import json
import re
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="ascii")).get("source_git_commit", "")
except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
    value = ""
if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40,64}", value):
    print(value)
PY
} 2>/dev/null)"
[[ -n "$source_sha" ]] && PUBLIC_SOURCE_SHA="$source_sha"

# Release-specific installers are data-plane inputs. The immutable systemd
# helper has already verified their recorded digest manifest before this call.
PLATFORM_ENABLE_SYSTEMD_UNITS=0 "$RESTORE_TOOL" \
  --app-dir "$APP_DIR" --release "$current_release" \
  --systemd-state "$SYSTEMD_STATE" --transaction "$STATE" \
  --live-qa-runtime-installer "$current_release/tools/platform_live_qa_runtime_install.py" \
  --systemctl "$SYSTEMCTL_BIN" --expected-csp-mode enforce \
  --edge-origin https://127.0.0.1 --edge-host old-sparky.com \
  --public-edge-origin https://old-sparky.com

/usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" verify \
  --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
  --transaction "$STATE" --helper-release "$current_release" \
  --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
  || { public_status failed systemd_state >&2; exit 1; }
run_nginx_config_test || { public_status failed nginx >&2; exit 1; }
for service in deadlock-api.service deadlock-worker.service deadlock-web.service; do
  expected_state="$({
    /usr/bin/python3 -I - "$SYSTEMD_STATE" "$service" <<'PY'
import json
import sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
for item in record["units"]:
    if item["name"] == sys.argv[2]:
        print(item["active"])
        break
else:
    raise SystemExit(1)
PY
  } 2>/dev/null)"
  case "$expected_state" in
    active|inactive)
      # ``systemctl is-active`` uses rc=0/active and rc=3/inactive.  Capture
      # the status explicitly: a negated command would turn timeout and every
      # other failure into success and could authorize receipt cleanup.
      actual_state=""
      actual_status=0
      if actual_state="$(run_systemctl is-active "$service" 2>/dev/null)"; then
        actual_status=0
      else
        actual_status="$?"
      fi
      case "$expected_state:$actual_status:$actual_state" in
        active:0:active|inactive:3:inactive) ;;
        *) public_status failed systemd_state >&2; exit 1 ;;
      esac
      ;;
    *) public_status failed systemd_state >&2; exit 1 ;;
  esac
done
/usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" clear \
  --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
  --transaction "$STATE" --helper-release "$current_release" \
  --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
  || { public_status failed systemd_state >&2; exit 1; }
test ! -e "$SYSTEMD_STATE" && test ! -L "$SYSTEMD_STATE" \
  || { public_status failed systemd_state >&2; exit 1; }
/usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery --state "$STATE" \
  >/dev/null 2>/dev/null \
  || { public_status failed transaction >&2; exit 1; }
test ! -e "$STATE" && test ! -L "$STATE" \
  || { public_status failed transaction >&2; exit 1; }
public_status passed
