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
RECOVERY_ACTION="recover_pending"
FAILED_DEPLOY_RUN_ID=""
FAILED_DEPLOY_RUN_ATTEMPT=""
FAILED_DEPLOY_SOURCE_SHA=""
RECOVERY_RUN_ID=""
RECOVERY_RUN_ATTEMPT=""
EXPECTED_CANDIDATES=()
PROTECTED_RELEASES=()
SOURCE_BOUND_LIVEQA_REUSE=0

public_status() {
  local status="$1" class="${2:-recovery}"
  printf 'RELEASE_RUNTIME schema=1 status=%s class=%s release_slug=%s source_sha=%s\n' \
    "$status" "$class" "$PUBLIC_RELEASE_SLUG" "$PUBLIC_SOURCE_SHA"
}

recovery_check_failed() {
  local check="$1"
  case "$check" in
    receipt_metadata|receipt_validate|capture_transaction|expected_receipt_current|verify_units|expected_unit_snapshot|unit_state|clear_receipt|receipt_absent)
      printf 'RECOVERY_CHECK schema=1 status=failed check=%s\n' "$check" >&2
      ;;
    *) return 1 ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --action)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      RECOVERY_ACTION="$2"; shift 2
      ;;
    --app-dir)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      APP_DIR="$2"; shift 2
      ;;
    --failed-deploy-run-id)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      FAILED_DEPLOY_RUN_ID="$2"; shift 2
      ;;
    --failed-deploy-run-attempt)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      FAILED_DEPLOY_RUN_ATTEMPT="$2"; shift 2
      ;;
    --failed-deploy-source-sha)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      FAILED_DEPLOY_SOURCE_SHA="$2"; shift 2
      ;;
    --recovery-run-id)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      RECOVERY_RUN_ID="$2"; shift 2
      ;;
    --recovery-run-attempt)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      RECOVERY_RUN_ATTEMPT="$2"; shift 2
      ;;
    --expected-candidate)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      EXPECTED_CANDIDATES+=("$2"); shift 2
      ;;
    --protected-release)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      PROTECTED_RELEASES+=("$2"); shift 2
      ;;
    --source-bound-liveqa-reuse)
      SOURCE_BOUND_LIVEQA_REUSE=1
      shift
      ;;
    --systemctl)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      SYSTEMCTL_BIN="$2"; shift 2
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_recover_pending.sh --app-dir <path> [--systemctl <absolute-path>]
       platform_recover_pending.sh --action quarantine-populated-quiesce
         --failed-deploy-run-id <id> --failed-deploy-run-attempt <attempt>
         --failed-deploy-source-sha <sha> --recovery-run-id <id>
         --recovery-run-attempt <attempt> --app-dir <path>
       platform_recover_pending.sh --action prune-task-releases
         --expected-candidate <slug>... --protected-release <slug>...
EOF
      exit 0
      ;;
    *) public_status failed argument >&2; exit 1 ;;
  esac
done

case "$RECOVERY_ACTION" in
  recover_pending) RECOVERY_CAPABILITY="recover_pending" ;;
  quarantine-populated-quiesce)
    RECOVERY_CAPABILITY="quarantine_populated_quiesce"
    [[ "$FAILED_DEPLOY_RUN_ID" =~ ^[1-9][0-9]{0,31}$ \
      && "$FAILED_DEPLOY_RUN_ATTEMPT" =~ ^[1-9][0-9]{0,31}$ \
      && "$FAILED_DEPLOY_SOURCE_SHA" =~ ^[0-9a-f]{40}$ \
      && "$RECOVERY_RUN_ID" =~ ^[1-9][0-9]{0,31}$ \
      && "$RECOVERY_RUN_ATTEMPT" =~ ^[1-9][0-9]{0,31}$ ]] \
      || { public_status failed argument >&2; exit 1; }
    ;;
  prune-task-releases)
    RECOVERY_CAPABILITY="prune_task_releases"
    [[ -z "$FAILED_DEPLOY_RUN_ID$FAILED_DEPLOY_RUN_ATTEMPT$FAILED_DEPLOY_SOURCE_SHA" \
      && -z "$RECOVERY_RUN_ID$RECOVERY_RUN_ATTEMPT" \
      && ${#EXPECTED_CANDIDATES[@]} -ge 1 && ${#EXPECTED_CANDIDATES[@]} -le 32 \
      && ${#PROTECTED_RELEASES[@]} -ge 11 && ${#PROTECTED_RELEASES[@]} -le 32 ]] \
      || { public_status failed argument >&2; exit 1; }
    ;;
  *) public_status failed argument >&2; exit 1 ;;
esac

if [[ "$SOURCE_BOUND_LIVEQA_REUSE" -eq 1 && "$RECOVERY_ACTION" != "recover_pending" ]]; then
  public_status failed argument >&2
  exit 1
fi

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
validate_generation_helper platform_release_retention.py 444
validate_generation_helper platform_release_capture_probe.py 444
validate_generation_helper platform_release_active_artifact_proof.py 444
/usr/bin/python3 -I -B "$TOOLS_DIR/platform_recovery_bootstrap.py" \
  validate-generation --generation "$TOOLS_DIR" --bundle-sha "$GENERATION_SHA" \
  --capability "$RECOVERY_CAPABILITY" \
  >/dev/null 2>/dev/null \
  || { public_status failed generation >&2; exit 1; }

if [[ "$RECOVERY_ACTION" == "prune-task-releases" ]]; then
  [[ "$APP_DIR" == "/opt/oldsparky/platform" ]] \
    || { public_status failed argument >&2; exit 1; }
  declare -A expected_seen=() protected_seen=()
  for slug in "${EXPECTED_CANDIDATES[@]}"; do
    [[ "$slug" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]] \
      || { public_status failed argument >&2; exit 1; }
    [[ -z "${expected_seen[$slug]:-}" ]] \
      || { public_status failed argument >&2; exit 1; }
    expected_seen["$slug"]=1
  done
  for slug in "${PROTECTED_RELEASES[@]}"; do
    [[ "$slug" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]] \
      || { public_status failed argument >&2; exit 1; }
    [[ -z "${protected_seen[$slug]:-}" ]] \
      || { public_status failed argument >&2; exit 1; }
    protected_seen["$slug"]=1
  done
  for slug in "${EXPECTED_CANDIDATES[@]}"; do
    [[ -z "${protected_seen[$slug]:-}" ]] \
      || { public_status failed argument >&2; exit 1; }
  done
  [[ ! -e "$APP_DIR/shared/.release-operation.json" \
    && ! -L "$APP_DIR/shared/.release-operation.json" \
    && ! -e "$APP_DIR/shared/.release-quiesce.json" \
    && ! -L "$APP_DIR/shared/.release-quiesce.json" \
    && ! -e "$APP_DIR/shared/.release-systemd-state.json" \
    && ! -L "$APP_DIR/shared/.release-systemd-state.json" ]] \
    || { public_status failed transaction >&2; exit 1; }
  [[ -L "$APP_DIR/current" && -L "$APP_DIR/previous" ]] \
    || { public_status failed topology >&2; exit 1; }
  current_before="$(readlink -f -- "$APP_DIR/current")" \
    || { public_status failed topology >&2; exit 1; }
  previous_before="$(readlink -f -- "$APP_DIR/previous")" \
    || { public_status failed topology >&2; exit 1; }
  [[ "$(dirname -- "$current_before")" == "$APP_DIR/releases" \
    && "$(dirname -- "$previous_before")" == "$APP_DIR/releases" ]] \
    || { public_status failed topology >&2; exit 1; }
  current_slug="${current_before##*/}"
  previous_slug="${previous_before##*/}"
  [[ -n "${protected_seen[$current_slug]:-}" \
    && -n "${protected_seen[$previous_slug]:-}" ]] \
    || { public_status failed argument >&2; exit 1; }
  for slug in "${PROTECTED_RELEASES[@]}"; do
    [[ -d "$APP_DIR/releases/$slug" && ! -L "$APP_DIR/releases/$slug" ]] \
      || { public_status failed topology >&2; exit 1; }
  done
  for service in deadlock-api deadlock-worker deadlock-web; do
    run_systemctl is-active --quiet "$service" \
      || { public_status failed service_state >&2; exit 1; }
  done
  run_nginx_config_test || { public_status failed nginx >&2; exit 1; }
  /usr/bin/curl --fail --silent --show-error --max-time 5 \
    http://127.0.0.1:8010/api/v1/health/ready >/dev/null 2>/dev/null \
    || { public_status failed service_state >&2; exit 1; }
  /usr/bin/curl --fail --silent --show-error --max-time 5 \
    http://127.0.0.1:3000/ >/dev/null 2>/dev/null \
    || { public_status failed service_state >&2; exit 1; }
  retention_args=(
    --app-dir "$APP_DIR" --keep 2 --min-age-days 0 --apply --json-receipt
  )
  for slug in "${EXPECTED_CANDIDATES[@]}"; do
    retention_args+=(--expected-candidate "$slug")
  done
  for slug in "${PROTECTED_RELEASES[@]}"; do
    retention_args+=(--protected-release "$slug")
  done
  retention_result="$(/usr/bin/python3 -I -B "$TOOLS_DIR/platform_release_retention.py" \
    "${retention_args[@]}" 2>/dev/null)" \
    || { public_status failed retention >&2; exit 1; }
  retention_counts="$(/usr/bin/python3 -I -B - "$retention_result" "${#EXPECTED_CANDIDATES[@]}" <<'PY'
import json
import sys
try:
    value = json.loads(sys.argv[1])
except (TypeError, ValueError):
    raise SystemExit(1)
expected = int(sys.argv[2])
keys = {"schema", "status", "candidate_count", "deleted_count", "reclaimed_bytes"}
if (
    not isinstance(value, dict) or set(value) != keys
    or type(value.get("schema")) is not int or value["schema"] != 1
    or value.get("status") != "passed"
    or type(value.get("candidate_count")) is not int or value["candidate_count"] != expected
    or type(value.get("deleted_count")) is not int or value["deleted_count"] != expected
    or type(value.get("reclaimed_bytes")) is not int or value["reclaimed_bytes"] < 0
):
    raise SystemExit(1)
print(f"{value['candidate_count']}\t{value['deleted_count']}\t{value['reclaimed_bytes']}")
PY
)" || { public_status failed retention >&2; exit 1; }
  IFS=$'\t' read -r retention_candidates retention_deleted retention_bytes <<<"$retention_counts"
  for slug in "${EXPECTED_CANDIDATES[@]}"; do
    [[ ! -e "$APP_DIR/releases/$slug" && ! -L "$APP_DIR/releases/$slug" ]] \
      || { public_status failed retention >&2; exit 1; }
  done
  for slug in "${PROTECTED_RELEASES[@]}"; do
    [[ -d "$APP_DIR/releases/$slug" && ! -L "$APP_DIR/releases/$slug" ]] \
      || { public_status failed retention >&2; exit 1; }
  done
  [[ "$(readlink -f -- "$APP_DIR/current")" == "$current_before" \
    && "$(readlink -f -- "$APP_DIR/previous")" == "$previous_before" ]] \
    || { public_status failed topology >&2; exit 1; }
  for service in deadlock-api deadlock-worker deadlock-web; do
    run_systemctl is-active --quiet "$service" \
      || { public_status failed service_state >&2; exit 1; }
  done
  /usr/bin/curl --fail --silent --show-error --max-time 5 \
    http://127.0.0.1:8010/api/v1/health/ready >/dev/null 2>/dev/null \
    || { public_status failed service_state >&2; exit 1; }
  /usr/bin/curl --fail --silent --show-error --max-time 5 \
    http://127.0.0.1:3000/ >/dev/null 2>/dev/null \
    || { public_status failed service_state >&2; exit 1; }
  printf 'PLATFORM_RETENTION schema=1 status=passed candidate_count=%s deleted_count=%s reclaimed_bytes=%s\n' \
    "$retention_candidates" "$retention_deleted" "$retention_bytes"
  public_status passed recovery
  exit 0
fi

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
CLOUDFLARE_PENDING_RESET_ATTEMPTED=0
CLOUDFLARE_PENDING_RESET_EVIDENCE_PATH=""
CLOUDFLARE_PENDING_RESET_ATTEMPT_NONCE=""
CLOUDFLARE_PENDING_RESET_EVIDENCE_ID=""
CLOUDFLARE_PENDING_RESET_TX_SHA256=""
CLOUDFLARE_PENDING_RESET_RECEIPT_SHA256=""
CLOUDFLARE_PENDING_RESET_INVOCATION_ID=""
platform_release_recovery_exit() {
  local original_status="$?" active_status=0 active_value="" enabled_status=0 enabled_value=""
  local evidence_bound=0 evidence_exists=0
  trap - EXIT
  if [[ "$CLOUDFLARE_PENDING_RESET_ATTEMPTED" == "1" ]]; then
    if [[ -n "$CLOUDFLARE_PENDING_RESET_EVIDENCE_PATH" \
      && -n "$CLOUDFLARE_PENDING_RESET_ATTEMPT_NONCE" \
      && -n "$CLOUDFLARE_PENDING_RESET_EVIDENCE_ID" ]]; then
      if [[ -e "$CLOUDFLARE_PENDING_RESET_EVIDENCE_PATH" || -L "$CLOUDFLARE_PENDING_RESET_EVIDENCE_PATH" ]]; then
        evidence_exists=1
        if /usr/bin/python3 -I - \
          "$CLOUDFLARE_PENDING_RESET_EVIDENCE_PATH" \
          "$CLOUDFLARE_PENDING_RESET_EVIDENCE_ID" \
          "$CLOUDFLARE_PENDING_RESET_ATTEMPT_NONCE" \
          "$operation_id" \
          "$CLOUDFLARE_PENDING_RESET_TX_SHA256" \
          "$CLOUDFLARE_PENDING_RESET_RECEIPT_SHA256" \
          "$CLOUDFLARE_PENDING_RESET_INVOCATION_ID" \
          "$source_sha" \
          "$previous_source_sha" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
evidence_id, nonce, operation_id, transaction_sha, receipt_sha, invocation_id, source_sha, previous_sha = sys.argv[2:]

def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value

try:
    parent = path.parent.lstat()
    before = path.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != 0 or parent.st_gid != 0 or stat.S_IMODE(parent.st_mode) != 0o700
        or not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
        or before.st_uid != 0 or before.st_gid != 0 or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 16 * 1024
        or path.name != evidence_id + ".json"
    ):
        raise ValueError
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_gid, opened.st_mode, opened.st_nlink, opened.st_size) != (
            before.st_dev, before.st_ino, before.st_uid, before.st_gid, before.st_mode, before.st_nlink, before.st_size
        ):
            raise ValueError
        chunks = []
        total = 0
        while total <= 16 * 1024:
            chunk = os.read(fd, min(4096, 16 * 1024 + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            chunks.append(chunk)
        after = os.fstat(fd)
        if total != before.st_size or total > 16 * 1024 or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError
    finally:
        os.close(fd)
    value = json.loads(b"".join(chunks).decode("ascii"), object_pairs_hook=_unique)
    service = value["service"]
    expected_id = hashlib.sha256(f"{operation_id}:{transaction_sha}:{receipt_sha}:{invocation_id}".encode("ascii")).hexdigest()
    if (
        expected_id != evidence_id or value.get("schema") != 1
        or value.get("kind") != "cloudflare_pending_guard_reset_evidence"
        or value.get("operation_id") != operation_id or value.get("attempt_nonce") != nonce
        or value.get("transaction_sha256") != transaction_sha
        or value.get("systemd_receipt_sha256") != receipt_sha
        or value.get("source_sha") != source_sha or value.get("previous_source_sha") != previous_sha
        or not isinstance(service, dict) or service.get("invocation_id") != invocation_id
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0)
PY
        then
          evidence_bound=1
        fi
      fi
    fi
    if [[ "$evidence_bound" == "1" ]]; then
      if active_value="$(run_systemctl is-active deadlock-cloudflare-ips.timer 2>/dev/null)"; then
        active_status=0
      else
        active_status="$?"
      fi
      if enabled_value="$(run_systemctl is-enabled deadlock-cloudflare-ips.timer 2>/dev/null)"; then
        enabled_status=0
      else
        enabled_status="$?"
      fi
      if [[ "$active_status:$active_value" == "3:inactive" \
        && "$enabled_status:$enabled_value" == "0:enabled" ]]; then
        run_systemctl start deadlock-cloudflare-ips.timer >/dev/null 2>/dev/null || true
        active_value=""
        if active_value="$(run_systemctl is-active deadlock-cloudflare-ips.timer 2>/dev/null)"; then
          active_status=0
        else
          active_status="$?"
        fi
      fi
      if [[ "$active_status:$active_value:$enabled_status:$enabled_value" != "0:active:0:enabled" ]]; then
        printf 'RECOVERY_TIMER_RESTORE schema=1 status=failed\n' >&2
        [[ "$original_status" -ne 0 ]] || original_status=1
      fi
    elif [[ "$evidence_exists" == "1" ]]; then
      # A file at the expected event path with different identity/content is
      # not authority to start a timer.
      printf 'RECOVERY_TIMER_RESTORE schema=1 status=failed\n' >&2
      [[ "$original_status" -ne 0 ]] || original_status=1
    fi
  fi
  platform_release_lock_close
  exit "$original_status"
}
trap platform_release_recovery_exit EXIT
if [[ -e "$STATE" || -L "$STATE" ]]; then
  test -f "$STATE" && test ! -L "$STATE" \
    && test "$(stat -c '%u:%h:%a' "$STATE" 2>/dev/null)" = "0:1:600" \
    || { public_status failed transaction >&2; exit 1; }
elif [[ "$RECOVERY_ACTION" != "quarantine-populated-quiesce" ]]; then
  public_status failed transaction >&2
  exit 1
fi

if [[ "$RECOVERY_ACTION" == "quarantine-populated-quiesce" ]]; then
  PUBLIC_RELEASE_SLUG="gha-${FAILED_DEPLOY_RUN_ID}-${FAILED_DEPLOY_RUN_ATTEMPT}-${FAILED_DEPLOY_SOURCE_SHA:0:12}"
  PUBLIC_SOURCE_SHA="$FAILED_DEPLOY_SOURCE_SHA"
  bundle_sha="$GENERATION_SHA"
  quarantine_args=(
    --state "$STATE"
    --failed-deploy-run-id "$FAILED_DEPLOY_RUN_ID"
    --failed-deploy-run-attempt "$FAILED_DEPLOY_RUN_ATTEMPT"
    --failed-deploy-source-sha "$FAILED_DEPLOY_SOURCE_SHA"
    --recovery-run-id "$RECOVERY_RUN_ID"
    --recovery-run-attempt "$RECOVERY_RUN_ATTEMPT"
    --bundle-sha "$bundle_sha"
  )
  if [[ ! -e "$STATE" && ! -L "$STATE" ]]; then
    resume_snapshot="$(/usr/bin/python3 -I "$TRANSACTION_TOOL" inspect-quarantined-quiesce \
      "${quarantine_args[@]}" 2>/dev/null)" \
      || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=resume_inspect\n' >&2; public_status failed transaction >&2; exit 1; }
    resume_phase="$(json_field phase <<<"$resume_snapshot")"
    [[ "$resume_phase" == services_restored || "$resume_phase" == receipt_consumed ]] \
      || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=resume_phase\n' >&2; public_status failed transaction >&2; exit 1; }
    run_nginx_config_test \
      || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=resume_nginx_validation\n' >&2; public_status failed nginx >&2; exit 1; }
    while IFS=$'\t' read -r unit expected_active expected_enabled; do
      [[ -n "$unit" ]] || continue
      actual=""; status=0
      if actual="$(run_systemctl is-active "$unit" 2>/dev/null)"; then status=0; else status="$?"; fi
      if [[ "$expected_active" == active ]]; then
        [[ "$status:$actual" == 0:active ]] || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=resume_service_state\n' >&2; public_status failed service_state >&2; exit 1; }
      else
        [[ "$status:$actual" == 3:inactive ]] || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=resume_service_state\n' >&2; public_status failed service_state >&2; exit 1; }
      fi
      actual=""; status=0
      if actual="$(run_systemctl is-enabled "$unit" 2>/dev/null)"; then status=0; else status="$?"; fi
      [[ "$actual" == "$expected_enabled" && ( "$status" == 0 || "$status" == 1 ) ]] \
        || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=resume_service_enabled\n' >&2; public_status failed service_state >&2; exit 1; }
    done < <(/usr/bin/python3 -I - "$resume_snapshot" <<'PY'
import json
import sys
value = json.loads(sys.argv[1])
for unit, active in value["service_state_before"].items():
    print(f"{unit}.service\t{active}\t{value['service_enabled_before'][unit]}")
print(f"deadlock-cloudflare-ips.timer\t{'active' if value['timer_active_before'] else 'inactive'}\t{value['timer_enabled_before']}")
PY
    )
    if [[ "$(json_field api <<<"$resume_snapshot")" == active ]]; then
      /usr/bin/curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8010/api/v1/health/ready >/dev/null 2>/dev/null \
        || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=resume_api_readiness\n' >&2; public_status failed readiness >&2; exit 1; }
    fi
    if [[ "$(json_field web <<<"$resume_snapshot")" == active ]]; then
      /usr/bin/curl --fail --silent --show-error --max-time 5 http://127.0.0.1:3000/ >/dev/null 2>/dev/null \
        || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=resume_web_readiness\n' >&2; public_status failed readiness >&2; exit 1; }
    fi
    /usr/bin/python3 -I "$TRANSACTION_TOOL" consume-quarantined-quiesce \
      "${quarantine_args[@]}" >/dev/null 2>/dev/null \
      || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=resume_consume\n' >&2; public_status failed transaction >&2; exit 1; }
    printf 'RECOVERY_QUARANTINE schema=1 status=passed phase=receipt_consumed\n'
    PUBLIC_RELEASE_SLUG="gha-${FAILED_DEPLOY_RUN_ID}-${FAILED_DEPLOY_RUN_ATTEMPT}-${FAILED_DEPLOY_SOURCE_SHA:0:12}"
    PUBLIC_SOURCE_SHA="$FAILED_DEPLOY_SOURCE_SHA"
    public_status passed recovery
    exit 0
  fi
  quarantine_result="$(/usr/bin/python3 -I "$TRANSACTION_TOOL" quarantine-populated-quiesce \
    "${quarantine_args[@]}" 2>/dev/null)" \
    || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=quarantine\n' >&2; public_status failed transaction >&2; exit 1; }
  quarantine_phase="$(json_field phase <<<"$quarantine_result")"
  if [[ "$quarantine_phase" == quarantined ]]; then
    /usr/bin/python3 -I "$TRANSACTION_TOOL" restore-quiesce \
      --state "$STATE" --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
      || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=service_restore\n' >&2; public_status failed service_state >&2; exit 1; }
  elif [[ "$quarantine_phase" != services_restored ]]; then
    printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=quarantine_phase\n' >&2
    public_status failed transaction >&2
    exit 1
  fi
  run_nginx_config_test \
    || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=nginx_validation\n' >&2; public_status failed nginx >&2; exit 1; }
  service_state="$({ /usr/bin/python3 -I - "$STATE" <<'PY'
import json
import sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
for key in ("service_state_before", "timer_active_before"):
    print(json.dumps(record[key], sort_keys=True, separators=(",", ":")))
PY
  } 2>/dev/null)" \
    || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=snapshot\n' >&2; public_status failed transaction >&2; exit 1; }
  mapfile -t expected_states <<<"$service_state"
  api_expected="$(/usr/bin/python3 -I -c 'import json,sys; print(json.loads(sys.stdin.read())["deadlock-api"])' <<<"${expected_states[0]}")"
  web_expected="$(/usr/bin/python3 -I -c 'import json,sys; print(json.loads(sys.stdin.read())["deadlock-web"])' <<<"${expected_states[0]}")"
  timer_expected="$(/usr/bin/python3 -I -c 'import json,sys; print("active" if json.loads(sys.stdin.read()) else "inactive")' <<<"${expected_states[1]}")"
  for unit in deadlock-api.service deadlock-worker.service deadlock-web.service; do
    actual=""
    status=0
    if actual="$(run_systemctl is-active "$unit" 2>/dev/null)"; then status=0; else status="$?"; fi
    case "$status:$actual" in 0:active|3:inactive) ;; *) printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=service_verify\n' >&2; public_status failed service_state >&2; exit 1 ;; esac
  done
  if [[ "$api_expected" == active ]]; then
    ready=0
    for attempt in {1..30}; do
      if /usr/bin/curl --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:8010/api/v1/health/ready >/dev/null 2>/dev/null; then ready=1; break; fi
      sleep 1
    done
    [[ "$ready" == 1 ]] || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=api_readiness\n' >&2; public_status failed readiness >&2; exit 1; }
  fi
  if [[ "$web_expected" == active ]]; then
    ready=0
    for attempt in {1..30}; do
      if /usr/bin/curl --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:3000/ >/dev/null 2>/dev/null; then ready=1; break; fi
      sleep 1
    done
    [[ "$ready" == 1 ]] || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=web_readiness\n' >&2; public_status failed readiness >&2; exit 1; }
  fi
  timer_actual=""
  timer_status=0
  if timer_actual="$(run_systemctl is-active deadlock-cloudflare-ips.timer 2>/dev/null)"; then timer_status=0; else timer_status="$?"; fi
  if [[ "$timer_expected:$timer_status:$timer_actual" != active:0:active \
    && "$timer_expected:$timer_status:$timer_actual" != inactive:3:inactive ]]; then
    printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=timer_verify\n' >&2
    public_status failed service_state >&2
    exit 1
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" mark-quarantine-services-restored \
    "${quarantine_args[@]}" >/dev/null 2>/dev/null \
    || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=receipt_update\n' >&2; public_status failed transaction >&2; exit 1; }
  /usr/bin/python3 -I "$TRANSACTION_TOOL" consume-quarantined-quiesce \
    "${quarantine_args[@]}" >/dev/null 2>/dev/null \
    || { printf 'RECOVERY_QUARANTINE schema=1 status=failed stage=receipt_consume\n' >&2; public_status failed transaction >&2; exit 1; }
  printf 'RECOVERY_QUARANTINE schema=1 status=passed phase=receipt_consumed\n'
  public_status passed recovery
  exit 0
fi

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
    || { recovery_check_failed receipt_metadata; public_status failed systemd_state >&2; exit 1; }
  /usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" validate \
    --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
    --transaction "$STATE" --helper-release "$original_current" \
    --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
    || { recovery_check_failed receipt_validate; public_status failed systemd_state >&2; exit 1; }
else
  # Reject null/partial snapshots before capture can query systemctl or write
  # the durable receipt; the operation receipt remains retained on failure.
  complete_service_snapshot \
    || { public_status failed service_state >&2; exit 1; }
  /usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" capture-transaction \
    --state "$SYSTEMD_STATE" --transaction "$STATE" --app-dir "$APP_DIR" \
    --helper-release "$original_current" --require-helper-manifest \
    --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
    || { recovery_check_failed capture_transaction; public_status failed systemd_state >&2; exit 1; }
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
} 2>/dev/null)" || { recovery_check_failed expected_receipt_current; public_status failed systemd_state >&2; exit 1; }
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

# A single install-recovery compatibility repair is permitted only when the
# already validated operation/companion pair proves the fixed 9ce -> f115
# target and the rollback receipt expects Cloudflare inactive/static while its
# timer remains active/enabled. All live state, journal, source-unit and
# timer-horizon checks remain in the immutable systemd helper.
if [[ "$transaction_phase" == recovery-restored \
  && "$pending_operation" == install \
  && "$current_release" == "$original_current" \
  && "$source_sha" == 9ceaf09ab7ba4bf5c5e636865106910eaa034c8d ]]; then
  previous_source_sha="$(/usr/bin/python3 -I - "$original_previous/RELEASE.json" <<'PY'
import json
import re
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="ascii")).get("source_git_commit", "")
except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
    value = ""
if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
    print(value)
PY
  2>/dev/null)"
  receipt_target="$({
    /usr/bin/python3 -I - "$SYSTEMD_STATE" <<'PY'
import json
import sys
from pathlib import Path
try:
    record = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)
units = record.get("units")
if not isinstance(units, list) or record.get("operation") != "rollback":
    raise SystemExit(1)
by_name = {item.get("name"): item for item in units if isinstance(item, dict)}
if len(by_name) != len(units):
    raise SystemExit(1)
if (
    by_name.get("deadlock-cloudflare-ips.service") == {
        "name": "deadlock-cloudflare-ips.service", "active": "inactive", "enabled": "static"
    }
    and by_name.get("deadlock-cloudflare-ips.timer") == {
        "name": "deadlock-cloudflare-ips.timer", "active": "active", "enabled": "enabled"
    }
):
    print("fixed-target")
PY
  } 2>/dev/null)"
  if [[ "$previous_source_sha" == f1154c7dba4f57bf52f7d7544e97d924c732be94 \
    && "$receipt_target" == fixed-target ]]; then
    CLOUDFLARE_PENDING_RESET_READY=0
    if CLOUDFLARE_PENDING_RESET_ATTEMPT_NONCE="$(/usr/bin/python3 -I -c 'import secrets; print(secrets.token_hex(32))')" \
      && [[ "$CLOUDFLARE_PENDING_RESET_ATTEMPT_NONCE" =~ ^[0-9a-f]{64}$ ]]; then
      CLOUDFLARE_PENDING_RESET_INVOCATION_ID="$(run_systemctl show --property=InvocationID --value deadlock-cloudflare-ips.service 2>/dev/null || true)"
    fi
    if [[ "$CLOUDFLARE_PENDING_RESET_INVOCATION_ID" =~ ^[0-9a-f]{32}$ ]]; then
      if binding_values="$(/usr/bin/python3 -I - "$STATE" "$SYSTEMD_STATE" <<'PY'
import hashlib
import os
import stat
import sys

def read_bound(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 1024 * 1024:
            raise SystemExit(1)
        data = bytearray()
        while len(data) <= 1024 * 1024:
            chunk = os.read(fd, min(65536, 1024 * 1024 + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        if len(data) != before.st_size or len(data) > 1024 * 1024 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise SystemExit(1)
        return bytes(data)
    finally:
        os.close(fd)

transaction, receipt = (read_bound(path) for path in sys.argv[1:])
print(hashlib.sha256(transaction).hexdigest(), hashlib.sha256(receipt).hexdigest())
PY
        )"; then
        read -r CLOUDFLARE_PENDING_RESET_TX_SHA256 CLOUDFLARE_PENDING_RESET_RECEIPT_SHA256 <<<"$binding_values"
        if [[ "$CLOUDFLARE_PENDING_RESET_TX_SHA256" =~ ^[0-9a-f]{64}$ \
          && "$CLOUDFLARE_PENDING_RESET_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
          CLOUDFLARE_PENDING_RESET_EVIDENCE_ID="$(/usr/bin/python3 -I -c 'import hashlib,sys; print(hashlib.sha256(":".join(sys.argv[1:]).encode("ascii")).hexdigest())' \
            "$operation_id" "$CLOUDFLARE_PENDING_RESET_TX_SHA256" \
            "$CLOUDFLARE_PENDING_RESET_RECEIPT_SHA256" "$CLOUDFLARE_PENDING_RESET_INVOCATION_ID")"
          if [[ "$CLOUDFLARE_PENDING_RESET_EVIDENCE_ID" =~ ^[0-9a-f]{64}$ ]]; then
            CLOUDFLARE_PENDING_RESET_EVIDENCE_PATH="$APP_DIR/shared/.release-cloudflare-pending-reset-evidence/$CLOUDFLARE_PENDING_RESET_EVIDENCE_ID.json"
            CLOUDFLARE_PENDING_RESET_READY=1
          fi
        fi
      fi
    fi
    if [[ "$CLOUDFLARE_PENDING_RESET_READY" == "1" ]]; then
      # Evidence is created only at the helper's final pre-stop boundary. The
      # EXIT trap accepts it only when its nonce and immutable bindings match.
      CLOUDFLARE_PENDING_RESET_ATTEMPTED=1
      helper_result=""
      if helper_result="$(/usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" reset-cloudflare-pending-guard \
        --state "$SYSTEMD_STATE" --transaction "$STATE" \
        --helper-release "$current_release" --app-dir "$APP_DIR" \
        --systemctl "$SYSTEMCTL_BIN" \
        --attempt-nonce "$CLOUDFLARE_PENDING_RESET_ATTEMPT_NONCE")"; then
        helper_status=0
      else
        helper_status="$?"
      fi
      if [[ "$helper_status" -ne 0 ]]; then
        public_status failed systemd_state >&2
        exit 1
      fi
      case "$helper_result" in
        'RELEASE_CLOUDFLARE_PENDING_RESET schema=1 status=skip')
          CLOUDFLARE_PENDING_RESET_ATTEMPTED=0
          ;;
        "RELEASE_CLOUDFLARE_PENDING_RESET schema=1 status=performed evidence_id=$CLOUDFLARE_PENDING_RESET_EVIDENCE_ID")
          [[ "$CLOUDFLARE_PENDING_RESET_ATTEMPTED" == "1" ]] \
            || { public_status failed systemd_state >&2; exit 1; }
          ;;
        *)
          public_status failed systemd_state >&2
          exit 1
          ;;
      esac
    fi
  fi
fi

# Release-specific installers are data-plane inputs. The immutable systemd
# helper has already verified their recorded digest manifest before this call.
source_bound_args=()
if [[ "$SOURCE_BOUND_LIVEQA_REUSE" -eq 1 ]]; then
  source_bound_args+=(--source-bound-liveqa-reuse)
fi
PLATFORM_ENABLE_SYSTEMD_UNITS=0 "$RESTORE_TOOL" \
  --app-dir "$APP_DIR" --release "$current_release" \
  --systemd-state "$SYSTEMD_STATE" --transaction "$STATE" \
  --live-qa-runtime-installer "$current_release/tools/platform_live_qa_runtime_install.py" \
  "${source_bound_args[@]}" \
  --systemctl "$SYSTEMCTL_BIN" --expected-csp-mode enforce \
  --edge-origin https://127.0.0.1 --edge-host old-sparky.com \
  --public-edge-origin https://old-sparky.com

/usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" verify \
  --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
  --transaction "$STATE" --helper-release "$current_release" \
  --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
  || { recovery_check_failed verify_units; public_status failed systemd_state >&2; exit 1; }
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
  } 2>/dev/null)" || { recovery_check_failed expected_unit_snapshot; public_status failed systemd_state >&2; exit 1; }
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
        *) recovery_check_failed unit_state; public_status failed systemd_state >&2; exit 1 ;;
      esac
      ;;
    *) recovery_check_failed unit_state; public_status failed systemd_state >&2; exit 1 ;;
  esac
done
/usr/bin/python3 -I "$SYSTEMD_STATE_TOOL" clear \
  --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
  --transaction "$STATE" --helper-release "$current_release" \
  --systemctl "$SYSTEMCTL_BIN" >/dev/null 2>/dev/null \
  || { recovery_check_failed clear_receipt; public_status failed systemd_state >&2; exit 1; }
test ! -e "$SYSTEMD_STATE" && test ! -L "$SYSTEMD_STATE" \
  || { recovery_check_failed receipt_absent; public_status failed systemd_state >&2; exit 1; }
/usr/bin/python3 -I "$TRANSACTION_TOOL" complete-recovery --state "$STATE" \
  >/dev/null 2>/dev/null \
  || { public_status failed transaction >&2; exit 1; }
test ! -e "$STATE" && test ! -L "$STATE" \
  || { public_status failed transaction >&2; exit 1; }
public_status passed
