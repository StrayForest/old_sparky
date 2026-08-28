#!/usr/bin/env bash
set +x
set -euo pipefail

TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_production_retained_load_cleanup_qa.sh"
QA_PYTHON="$PLATFORM_ROOT/.venv_platform/bin/python"
RUNTIME_ROOT="/opt/oldsparky/platform"
RUN_ROOT_BASE="$RUNTIME_ROOT/shared/production-retained-matrix"
SYSTEM_PYTHON="/usr/bin/python3.12"
CONFIRMATION="DELETE-PRODUCTION-RETAINED-LOAD"
LOCK_PATH="/run/lock/oldsparky-retained-load-matrix.lock"
EXPECTED_ORIGIN="https://old-sparky.com"

if [[ "$EUID" -ne 0 ]]; then
  echo "Production retained load cleanup supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Production retained cleanup must run from the fixed root-controlled checkout." >&2
  exit 1
fi
exec 9>"$LOCK_PATH"
flock -n 9 || {
  echo "Another retained load or cleanup operation is already running on this host." >&2
  exit 1
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
[[ "$load_run_id" =~ ^[0-9]+$ && "$cleanup_run_id" =~ ^[0-9]+$ ]] || {
  echo "GitHub run ids must be numeric." >&2
  exit 1
}
[[ "$control_email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || {
  echo "Control email is invalid." >&2
  exit 1
}

test -d "$TRUSTED_REPO_ROOT/.git" || {
  echo "Trusted production checkout is missing." >&2
  exit 1
}
test -x "$QA_PYTHON" || {
  echo "Production cleanup Python runtime is missing." >&2
  exit 1
}
test -L "$RUNTIME_ROOT/current" || {
  echo "Active production release is missing." >&2
  exit 1
}
checkout_sha="$(git -C "$TRUSTED_REPO_ROOT" rev-parse --verify HEAD)"
test "$checkout_sha" = "$target_sha" || {
  echo "Trusted production checkout does not match the cleanup workflow SHA." >&2
  exit 1
}
release_sha="$($SYSTEM_PYTHON -I - "$RUNTIME_ROOT/current/RELEASE.json" <<'PY'
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
platform_environment="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_ENVIRONMENT)"
test "$platform_environment" = "production" || {
  echo "Production retained cleanup requires PLATFORM_ENVIRONMENT=production." >&2
  exit 1
}
platform_origin="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_WEB_ORIGIN)"
test "$platform_origin" = "$EXPECTED_ORIGIN" || {
  echo "Production retained cleanup requires the canonical production origin." >&2
  exit 1
}

run_root="$RUN_ROOT_BASE/gha-$load_run_id"
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
test "$run_root_uid" = "0" && test "$run_root_mode" = "700" || {
  echo "The selected retained load run root must be root-owned mode 0700." >&2
  exit 1
}
shopt -s nullglob
summaries=("$run_root"/*/matrix-summary.json)
shopt -u nullglob
if (( ${#summaries[@]} != 1 )); then
  # A coordinator can fail before publishing its cleanup inventory (for
  # example during argument validation).  Treat that exact, root-owned run
  # directory as a safe no-op after confirming no fixture/control evidence
  # exists.  A published control or report always follows the manifest path
  # below and still requires the full identity-checked cleanup.
  if [[ ! -e "$run_root/control.json" \
    && ! -e "$run_root/event-triggered.json" \
    && ! -e "$run_root/matrix-summary.json" ]]; then
    if find "$run_root" -type l -print -quit | grep -q .; then
      echo "The selected partial retained load run contains an unexpected symlink." >&2
      exit 1
    fi
    rm -rf -- "$run_root"
    partial_export_uid="${SUDO_UID:-0}"
    partial_export_gid="${SUDO_GID:-0}"
    [[ "$partial_export_uid" =~ ^[0-9]+$ && "$partial_export_gid" =~ ^[0-9]+$ ]] || {
      echo "Unable to determine the SSH caller identity for partial cleanup export." >&2
      exit 1
    }
    partial_export_dir="/tmp/old-sparky-production-retained-cleanup-$cleanup_run_id"
    rm -rf -- "$partial_export_dir"
    install -d -o root -g root -m 0700 "$partial_export_dir"
    printf '%s\n' "No fixture inventory was published; removed exact partial run root: $run_root" \
      > "$partial_export_dir/cleanup.log"
    printf '%s\n' '{"ok":true,"markers":0,"users_deleted":0,"tournaments_deleted":0,"control_account_preserved":true,"partial_run_root_removed":true}' \
      > "$partial_export_dir/cleanup-summary.json"
    chown -R "$partial_export_uid:$partial_export_gid" "$partial_export_dir"
    chmod 0700 "$partial_export_dir"
    chmod 0600 "$partial_export_dir/cleanup.log" "$partial_export_dir/cleanup-summary.json"
    printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXPORT=%s\n' "$partial_export_dir"
    printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_SUMMARY=%s\n' "$partial_export_dir/cleanup-summary.json"
    printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXIT_CODE=0\n'
    printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_OK=1\n'
    echo "No fixture inventory was published; removed the exact partial run root."
    exit 0
  fi
  profile_count=0
  recovery_profile=""
  for candidate_profile in read-mix write-burst; do
    if [[ -d "$run_root/$candidate_profile" ]]; then
      profile_count=$((profile_count + 1))
      recovery_profile="$candidate_profile"
    fi
  done
  if (( profile_count == 1 )); then
    "$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" exec \
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
export_uid="${SUDO_UID:-0}"
export_gid="${SUDO_GID:-0}"
[[ "$export_uid" =~ ^[0-9]+$ && "$export_gid" =~ ^[0-9]+$ ]] || {
  echo "Unable to determine the SSH caller identity for cleanup export." >&2
  exit 1
}
rm -rf -- "$export_dir"
install -d -o root -g root -m 0700 "$export_dir"
log_path="$export_dir/cleanup.log"
result_path="$export_dir/cleanup-summary.json"

set +e
"$SYSTEM_PYTHON" -I "$TOOLS_DIR/platform_safe_env_exec.py" exec \
  --pythonpath "$PLATFORM_ROOT" \
  -- "$QA_PYTHON" "$TOOLS_DIR/platform_cleanup_retained_matrix.py" \
  --summary "$summary_path" \
  --run-root "$run_root" \
  --control-email "$control_email" \
  --confirm "$CONFIRMATION" \
  --result-path "$result_path" \
  2>&1 | tee "$log_path"
pipeline_status=("${PIPESTATUS[@]}")
cleanup_status="${pipeline_status[0]}"
tee_status="${pipeline_status[1]}"
set -e
if [[ "$tee_status" != "0" ]]; then
  cleanup_status=1
fi
if [[ "$cleanup_status" == "0" ]]; then
  test -s "$result_path" || {
    echo "Cleanup returned success without a result manifest." >&2
    cleanup_status=1
  }
fi
if [[ "$cleanup_status" == "0" ]]; then
  rm -rf -- "$run_root"
fi
chown -R "$export_uid:$export_gid" "$export_dir"
chmod 0700 "$export_dir"
chmod 0600 "$export_dir/cleanup.log" "$export_dir/cleanup-summary.json" 2>/dev/null || true
printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXPORT=%s\n' "$export_dir"
printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_SUMMARY=%s\n' "$export_dir/cleanup-summary.json"
printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_EXIT_CODE=%s\n' "$cleanup_status"
if [[ "$cleanup_status" == "0" ]]; then
  printf 'PRODUCTION_RETAINED_LOAD_CLEANUP_OK=1\n'
fi
exit "$cleanup_status"
