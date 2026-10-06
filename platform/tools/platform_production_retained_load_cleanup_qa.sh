#!/usr/bin/env bash
set +x
set -euo pipefail

RUNTIME_ROOT="/opt/oldsparky/platform"
PLATFORM_ROOT="$RUNTIME_ROOT/current"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$(readlink -f -- "$TOOLS_DIR/platform_production_retained_load_cleanup_qa.sh")"
QA_PYTHON="$RUNTIME_ROOT/shared/venv/bin/python"
RUN_ROOT_BASE="$RUNTIME_ROOT/shared/production-retained-matrix"
SYSTEM_PYTHON="/usr/bin/python3.12"
CONFIRMATION="DELETE-PRODUCTION-RETAINED-LOAD"
EXPECTED_ORIGIN="https://old-sparky.com"

if [[ "$EUID" -ne 0 ]]; then
  echo "Production retained load cleanup supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Production retained cleanup must run from the active immutable release." >&2
  exit 1
fi
LOCK_HELPER="$TOOLS_DIR/platform_release_lock.sh"
if [[ ! -f "$LOCK_HELPER" || -L "$LOCK_HELPER" || ! -x "$LOCK_HELPER" ]]; then
  echo "Canonical retained-load lock helper is missing or unsafe." >&2
  exit 1
fi
# The helper is selected only after the active immutable release path has been
# validated above; ShellCheck cannot resolve that runtime-derived source path.
# shellcheck source=/dev/null
source "$LOCK_HELPER"
ORIGINAL_ARGS=("$@")
cleanup_stage_emit() {
  local stage="$1" exit_code="$2"
  case "$stage" in
    lock|input|identity|release_binding|run_root|external_vote_recovery|orphan_cleanup|matrix_cleanup|export_cleanup|complete) ;;
    *) stage="unknown" ;;
  esac
  [[ "$exit_code" =~ ^(0|[1-9][0-9]{0,2})$ ]] || exit_code=255
  if (( exit_code > 255 )); then exit_code=255; fi
  printf 'RETAINED_CLEANUP_STAGE schema=1 stage=%s exit_code=%s\n' "$stage" "$exit_code"
}
if platform_retained_load_lock_supervise "${ORIGINAL_ARGS[@]}"; then
  :
else
  lock_status="$?"
  cleanup_stage_emit lock "$lock_status"
  echo "Another retained load or cleanup operation is already running on this host." >&2
  exit "$lock_status"
fi
if [[ "${PLATFORM_RETAINED_LOAD_LOCK_SUPERVISED:-}" != "1" ]]; then
  exit 0
fi
if platform_retained_load_lock_open; then
  :
else
  lock_status="$?"
  cleanup_stage_emit lock "$lock_status"
  echo "Retained-load lock supervisor could not be validated." >&2
  exit "$lock_status"
fi
CLEANUP_STAGE="input"
# Invoked indirectly by the registered EXIT trap.
# shellcheck disable=SC2317
cleanup_exit_report() {
  local exit_code="$?"
  trap - EXIT
  platform_retained_load_lock_close >/dev/null 2>&1 || true
  cleanup_stage_emit "${CLEANUP_STAGE:-input}" "$exit_code"
  return "$exit_code"
}
trap cleanup_exit_report EXIT
run_external_vote_recovery() {
  CLEANUP_STAGE="external_vote_recovery"
  "$@" >/dev/null 2>&1
}
if (( $# != 5 )) || [[ "$1" != "$CONFIRMATION" ]]; then
  echo "Usage: $0 $CONFIRMATION <target-sha> <load-run-id> <control-email> <cleanup-run-id>" >&2
  exit 2
fi

confirmation="$1"
target_sha="$2"
load_run_id="$3"
control_email="$4"
cleanup_run_id="$5"

[[ "$confirmation" == "$CONFIRMATION" ]]
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Target SHA must be a lowercase 40-character commit SHA." >&2
  exit 1
}
[[ "$load_run_id" =~ ^[1-9][0-9]{0,31}$ && "$cleanup_run_id" =~ ^[1-9][0-9]{0,31}$ ]] || {
  echo "GitHub run ids must be numeric." >&2
  exit 1
}
"$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_workflow_input_guard.py" email \
  --value "$control_email" || {
  echo "Control email is invalid." >&2
  exit 1
}

legacy_export_uid="${SUDO_UID:-0}"
CLEANUP_STAGE="identity"
[[ "$legacy_export_uid" =~ ^[0-9]+$ ]] || {
  echo "Unable to validate the legacy retained-load export owner." >&2
  exit 1
}
artifact_owner_json="$("$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_retained_load_export_executor.py" owner)" || {
  echo "Dedicated retained-load artifact owner is not available." >&2
  exit 1
}
artifact_owner_pair="$("$SYSTEM_PYTHON" -I -B -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (TypeError, json.JSONDecodeError):
    raise SystemExit(1)
if (
    not isinstance(payload, dict)
    or set(payload) != {"uid", "gid"}
    or type(payload.get("uid")) is not int
    or type(payload.get("gid")) is not int
    or payload["uid"] <= 0
    or payload["gid"] <= 0
):
    raise SystemExit(1)
print("{} {}".format(payload["uid"], payload["gid"]))
' <<< "$artifact_owner_json")" || {
  echo "Dedicated retained-load artifact owner projection is invalid." >&2
  exit 1
}
read -r export_uid export_gid <<< "$artifact_owner_pair"

external_load_export_dir="/tmp/old-sparky-production-retained-load-$load_run_id"
remove_external_load_export() {
  if [[ ! -e "$external_load_export_dir" && ! -L "$external_load_export_dir" ]]; then
    return 0
  fi
  if [[ -L "$external_load_export_dir" || ! -d "$external_load_export_dir" ]]; then
    echo "Refusing external retained-load export removal with an unexpected path type." >&2
    return 1
  fi
  if find "$external_load_export_dir" -xdev \
    \( -type l -o ! \( -type f -o -type d \) \
      -o ! \( -user "$legacy_export_uid" -o -user "$export_uid" -o -user 0 \) \
      -o -perm /022 -o \( -type f ! -links 1 \) \) \
    -print -quit | grep -q .; then
    echo "Refusing external retained-load export removal with unexpected ownership, mode, inode, or symlink." >&2
    return 1
  fi
  if [[ "$(stat -c '%u' -- "$external_load_export_dir")" == "$export_uid" ]]; then
    # Dedicated-owner exports remain available through report projection and
    # transfer; the pinned host-tools wrapper removes both exact exports only
    # after it has validated and copied them.
    return 0
  fi
  rm -rf -- "$external_load_export_dir"
}

test -x "$QA_PYTHON" || {
  echo "Production cleanup Python runtime is missing." >&2
  exit 1
}
test -L "$RUNTIME_ROOT/current" || {
  echo "Active production release is missing." >&2
  exit 1
}
CLEANUP_STAGE="release_binding"
release_sha="$($SYSTEM_PYTHON -I -B - "$RUNTIME_ROOT/current/RELEASE.json" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload.get("source_git_commit", ""))
PY
)"
test "$release_sha" = "$target_sha" || {
  echo "Active production release does not match the cleanup workflow SHA." >&2
  exit 1
}
platform_environment="$($SYSTEM_PYTHON -I -B "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_ENVIRONMENT)"
test "$platform_environment" = "production" || {
  echo "Production retained cleanup requires PLATFORM_ENVIRONMENT=production." >&2
  exit 1
}
platform_origin="$($SYSTEM_PYTHON -I -B "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_WEB_ORIGIN)"
test "$platform_origin" = "$EXPECTED_ORIGIN" || {
  echo "Production retained cleanup requires the canonical production origin." >&2
  exit 1
}

CLEANUP_STAGE="run_root"
run_root="$RUN_ROOT_BASE/gha-$load_run_id"
if [[ -L "$run_root" ]]; then
  echo "The selected retained load run root must not be a symlink." >&2
  exit 1
fi
if [[ ! -e "$run_root" ]]; then
  export_dir="/tmp/old-sparky-production-retained-cleanup-$cleanup_run_id"
  if [[ -e "$export_dir" || -L "$export_dir" ]]; then
    echo "A retained-load cleanup export already exists for this cleanup run id." >&2
    exit 1
  fi
  /usr/bin/mkdir -m 0700 -- "$export_dir"
  /usr/bin/chown "$export_uid:$export_gid" -- "$export_dir"
  /usr/bin/chmod 0700 -- "$export_dir"
  log_path="$export_dir/canonical.log"
  raw_log_path="$export_dir/cleanup-raw.log"
  result_path="$export_dir/cleanup-summary.json"
  CLEANUP_STAGE="orphan_cleanup"
  set +e
  "$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_safe_env_exec.py" exec \
    --pythonpath "$PLATFORM_ROOT" \
    -- "$QA_PYTHON" "$TOOLS_DIR/platform_cleanup_retained_orphan.py" \
    --load-run-id "$load_run_id" \
    --control-email "$control_email" \
    --confirm "$CONFIRMATION" \
    --result-path "$result_path" \
    > "$raw_log_path" 2>&1
  cleanup_status="$?"
  set -e
  "$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_evidence_sanitizer.py" \
    --input "$raw_log_path" --output "$log_path"
  rm -f -- "$raw_log_path"
  test ! -e "$raw_log_path"
  if [[ "$cleanup_status" == "0" ]]; then
    test -s "$result_path" || {
      echo "Orphan cleanup returned success without a result manifest." >&2
      cleanup_status=1
    }
  fi
  if [[ "$cleanup_status" == "0" ]] && ! remove_external_load_export; then
    cleanup_status=1
  fi
  chown "$export_uid:$export_gid" "$log_path"
  if [[ -f "$result_path" && ! -L "$result_path" ]]; then
    chown "$export_uid:$export_gid" "$result_path"
  fi
  chmod 0600 "$log_path" "$result_path" 2>/dev/null || true
  printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXPORT=%s\n' "$export_dir"
  printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_SUMMARY=%s\n' "$result_path"
  printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXIT_CODE=%s\n' "$cleanup_status"
  if [[ "$cleanup_status" == "0" ]]; then
    CLEANUP_STAGE="complete"
    printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_OK=1\n'
  fi
  exit "$cleanup_status"
fi
test -d "$run_root" || {
  echo "The selected retained load run root does not exist." >&2
  exit 1
}
test ! -L "$run_root" || {
  echo "The selected retained load run root must not be a symlink." >&2
  exit 1
}
run_root_uid="$(stat -c '%u' -- "$run_root")"
run_root_mode="$(stat -c '%a' -- "$run_root")"
if [[ "$run_root_uid" == "0" ]]; then
  # Recovery fixtures created by the pre-0700 supervisor may have inherited
  # the default mode on an intermediate directory. Normalize that exact,
  # already root-owned non-symlink path before enforcing the invariant.
  chmod 0700 -- "$run_root"
  run_root_mode="$(stat -c '%a' -- "$run_root")"
fi
if [[ "$run_root_uid" != "0" || "$run_root_mode" != "700" ]]; then
  echo "The selected retained load run root must be root-owned mode 0700." >&2
  exit 1
fi
shopt -s nullglob
summaries=("$run_root"/*/matrix-summary.json)
shopt -u nullglob
# Recovery is also required when a supervisor published a summary but was
# interrupted before its detail report was written.  The external-vote
# supervisor uses a transport-specific directory while its durable QA row
# remains mode=write-burst, so it needs the same exact identity recovery.
recovery_profile=""
profile_count=0
for candidate_profile in read-mix write-burst external-vote; do
  if [[ -d "$run_root/$candidate_profile" ]]; then
    profile_count=$((profile_count + 1))
    recovery_profile="$candidate_profile"
  fi
done
recovery_needed=0
if (( ${#summaries[@]} != 1 )); then
  recovery_needed=1
elif (( profile_count == 1 )) && [[ ! -f "$run_root/$recovery_profile/$recovery_profile.json" ]]; then
  recovery_needed=1
fi
if (( recovery_needed == 1 )) || {
  [[ "$recovery_profile" == "external-vote" ]] && (( profile_count == 1 ));
}; then
  # A coordinator can fail before publishing its cleanup inventory (for
  # example during argument validation).  Treat that exact, root-owned run
  # directory as a safe no-op after confirming no fixture/control evidence
  # exists.  A published control or report always follows the manifest path
  # below and still requires the full identity-checked cleanup.
  if (( profile_count == 0 )) && [[ ! -e "$run_root/control.json" \
    && ! -e "$run_root/event-triggered.json" \
    && ! -e "$run_root/matrix-summary.json" ]]; then
    if find "$run_root" -type l -print -quit | grep -q .; then
      echo "The selected partial retained load run contains an unexpected symlink." >&2
      exit 1
    fi
    CLEANUP_STAGE="export_cleanup"
    remove_external_load_export
    rm -rf -- "$run_root"
    partial_export_dir="/tmp/old-sparky-production-retained-cleanup-$cleanup_run_id"
    if [[ -e "$partial_export_dir" || -L "$partial_export_dir" ]]; then
      echo "A retained-load cleanup export already exists for this cleanup run id." >&2
      exit 1
    fi
    /usr/bin/mkdir -m 0700 -- "$partial_export_dir"
    /usr/bin/chown "$export_uid:$export_gid" -- "$partial_export_dir"
    /usr/bin/chmod 0700 -- "$partial_export_dir"
    printf '%s\n' '{"schema":1,"status":"passed","event":"partial_run_root_removed"}' \
      > "$partial_export_dir/canonical.log"
    printf '%s\n' '{"ok":true,"markers":0,"users_deleted":0,"tournaments_deleted":0,"control_account_preserved":true,"partial_run_root_removed":true}' \
      > "$partial_export_dir/cleanup-summary.json"
    chown "$export_uid:$export_gid" "$partial_export_dir/canonical.log" "$partial_export_dir/cleanup-summary.json"
    chmod 0600 "$partial_export_dir/canonical.log" "$partial_export_dir/cleanup-summary.json"
    printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXPORT=%s\n' "$partial_export_dir"
    printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_SUMMARY=%s\n' "$partial_export_dir/cleanup-summary.json"
    printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXIT_CODE=0\n'
    CLEANUP_STAGE="complete"
    printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_OK=1\n'
    echo "No fixture inventory was published; removed the exact partial run root."
    exit 0
  fi
  if (( profile_count == 1 )); then
    run_external_vote_recovery "$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_safe_env_exec.py" exec \
      --pythonpath "$PLATFORM_ROOT" \
      -- "$QA_PYTHON" "$TOOLS_DIR/platform_recover_retained_report.py" \
      --run-root "$run_root" \
      --load-run-id "$load_run_id" \
      --control-email "$control_email" \
      --mode "$recovery_profile"
  fi
  shopt -s nullglob
  summaries=("$run_root"/*/matrix-summary.json)
  shopt -u nullglob
fi
CLEANUP_STAGE="matrix_cleanup"
if (( ${#summaries[@]} != 1 )); then
  echo "The selected load run must contain exactly one matrix summary." >&2
  exit 1
fi
summary_path="${summaries[0]}"
test -f "$summary_path" || {
  echo "The selected matrix summary is missing." >&2
  exit 1
}

export_dir="/tmp/old-sparky-production-retained-cleanup-$cleanup_run_id"
if [[ -e "$export_dir" || -L "$export_dir" ]]; then
  echo "A retained-load cleanup export already exists for this cleanup run id." >&2
  exit 1
fi
/usr/bin/mkdir -m 0700 -- "$export_dir"
/usr/bin/chown "$export_uid:$export_gid" -- "$export_dir"
/usr/bin/chmod 0700 -- "$export_dir"
log_path="$export_dir/canonical.log"
raw_log_path="$export_dir/cleanup-raw.log"
result_path="$export_dir/cleanup-summary.json"

set +e
"$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_safe_env_exec.py" exec \
  --pythonpath "$PLATFORM_ROOT" \
  -- "$QA_PYTHON" "$TOOLS_DIR/platform_cleanup_retained_matrix.py" \
  --summary "$summary_path" \
  --run-root "$run_root" \
  --control-email "$control_email" \
  --confirm "$CONFIRMATION" \
  --result-path "$result_path" \
  > "$raw_log_path" 2>&1
cleanup_status="$?"
set -e
"$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_evidence_sanitizer.py" \
  --input "$raw_log_path" --output "$log_path"
rm -f -- "$raw_log_path"
test ! -e "$raw_log_path"
if [[ "$cleanup_status" == "0" ]]; then
  test -s "$result_path" || {
    echo "Cleanup returned success without a result manifest." >&2
    cleanup_status=1
  }
fi
if [[ "$cleanup_status" == "0" ]]; then
  if find "$run_root" -xdev \( -type l -o ! -user 0 -o -perm /022 \) -print -quit | grep -q .; then
    echo "Refusing retained run root removal with unexpected ownership, mode, or symlink." >&2
    cleanup_status=1
  fi
fi
if [[ "$cleanup_status" == "0" ]]; then
  CLEANUP_STAGE="export_cleanup"
  if ! remove_external_load_export; then
    cleanup_status=1
  fi
fi
if [[ "$cleanup_status" == "0" ]]; then
  rm -rf -- "$run_root"
fi
chown "$export_uid:$export_gid" "$export_dir/canonical.log"
if [[ -f "$export_dir/cleanup-summary.json" && ! -L "$export_dir/cleanup-summary.json" ]]; then
  chown "$export_uid:$export_gid" "$export_dir/cleanup-summary.json"
fi
chmod 0600 "$export_dir/canonical.log" "$export_dir/cleanup-summary.json" 2>/dev/null || true
printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXPORT=%s\n' "$export_dir"
printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_SUMMARY=%s\n' "$export_dir/cleanup-summary.json"
printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXIT_CODE=%s\n' "$cleanup_status"
if [[ "$cleanup_status" == "0" ]]; then
  printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_OK=1\n'
  CLEANUP_STAGE="complete"
fi
exit "$cleanup_status"
