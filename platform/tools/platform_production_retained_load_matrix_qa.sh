#!/usr/bin/env bash
set +x
set -euo pipefail

TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_production_retained_load_matrix_qa.sh"
QA_PYTHON="$PLATFORM_ROOT/.venv_platform/bin/python"
RUNTIME_ROOT="/opt/oldsparky/platform"
OUTPUT_ROOT_BASE="$RUNTIME_ROOT/shared/production-retained-matrix"
SYSTEM_PYTHON="/usr/bin/python3.12"
CONFIRMATION="RUN-PRODUCTION-RETAINED-LOAD-MATRIX"
LOCK_PATH="/run/lock/oldsparky-retained-load-matrix.lock"
EXPECTED_ORIGIN="https://old-sparky.com"
MAX_RUNTIME="180m"

if [[ "$EUID" -ne 0 ]]; then
  echo "Production retained load matrix supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Production retained load must run from the fixed root-controlled checkout." >&2
  exit 1
fi
exec 9>"$LOCK_PATH"
flock -n 9 || {
  echo "Another retained load or cleanup operation is already running on this host." >&2
  exit 1
}
if (( $# != 5 && $# != 6 )) || [[ "$1" != "$CONFIRMATION" ]]; then
  echo "Usage: $0 $CONFIRMATION <target-sha> <control-email> <concurrency> <run-id> [matrix|browser-polling]" >&2
  exit 2
fi

confirmation="$1"
target_sha="$2"
control_email="$3"
concurrency="$4"
run_id="$5"
profile="${6:-matrix}"

case "$profile" in
  matrix|browser-polling) ;;
  *)
    echo "Profile must be matrix or browser-polling." >&2
    exit 1
    ;;
esac

[[ "$confirmation" == "$CONFIRMATION" ]]
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Target SHA must be a lowercase 40-character commit SHA." >&2
  exit 1
}
[[ "$control_email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || {
  echo "Control email is invalid." >&2
  exit 1
}
[[ "$concurrency" =~ ^[1-9][0-9]{0,2}$ ]] && (( concurrency <= 256 )) || {
  echo "Concurrency must be an integer from 1 to 256." >&2
  exit 1
}
[[ "$run_id" =~ ^[0-9]+$ ]] || {
  echo "Run id must be numeric." >&2
  exit 1
}

test -d "$TRUSTED_REPO_ROOT/.git" || {
  echo "Trusted production checkout is missing." >&2
  exit 1
}
test -x "$QA_PYTHON" || {
  echo "Production QA Python runtime is missing." >&2
  exit 1
}
test -L "$RUNTIME_ROOT/current" || {
  echo "Active production release is missing." >&2
  exit 1
}

checkout_sha="$(git -C "$TRUSTED_REPO_ROOT" rev-parse --verify HEAD)"
test "$checkout_sha" = "$target_sha" || {
  echo "Trusted production checkout does not match the dispatched target SHA." >&2
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
  echo "Active production release does not match the dispatched target SHA." >&2
  exit 1
}

platform_environment="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_ENVIRONMENT)"
test "$platform_environment" = "production" || {
  echo "Production retained load requires PLATFORM_ENVIRONMENT=production." >&2
  exit 1
}
platform_origin="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_WEB_ORIGIN)"
test "$platform_origin" = "$EXPECTED_ORIGIN" || {
  echo "Production retained load requires the canonical production origin." >&2
  exit 1
}

run_root="$OUTPUT_ROOT_BASE/gha-$run_id"
export_dir="/tmp/old-sparky-production-retained-load-$run_id"
export_uid="${SUDO_UID:-0}"
export_gid="${SUDO_GID:-0}"
[[ "$export_uid" =~ ^[0-9]+$ && "$export_gid" =~ ^[0-9]+$ ]] || {
  echo "Unable to determine the SSH caller identity for report export." >&2
  exit 1
}
if [[ -e "$run_root" || -L "$run_root" ]]; then
  echo "A production retained run already exists for this GitHub run id." >&2
  exit 1
fi

install -d -o root -g root -m 0700 "$OUTPUT_ROOT_BASE"
install -d -o root -g root -m 0700 "$run_root"
rm -rf -- "$export_dir"
install -d -o "$export_uid" -g "$export_gid" -m 0700 "$export_dir"
log_path="$run_root/matrix.log"

if [[ "$profile" == "matrix" ]]; then
  set +e
  timeout --signal=TERM --kill-after=30s "$MAX_RUNTIME" \
  "$QA_PYTHON" "$TOOLS_DIR/platform_seed_retained_tournament_matrix.py" \
    --origin "$EXPECTED_ORIGIN" \
    --control-email "$control_email" \
    --concurrency "$concurrency" \
    --output-root "$run_root" \
    2>&1 | tee "$log_path"
  pipeline_status=("${PIPESTATUS[@]}")
  qa_status="${pipeline_status[0]}"
  tee_status="${pipeline_status[1]}"
  set -e
  if [[ "$tee_status" != "0" ]]; then
    qa_status=1
  fi
else
  browser_root="$run_root/browser-polling"
  install -d -o root -g root -m 0700 "$browser_root"
  browser_report="$browser_root/browser-polling.json"
  browser_http_connections=$((concurrency * 4))
  if (( browser_http_connections < 128 )); then
    browser_http_connections=128
  elif (( browser_http_connections > 512 )); then
    browser_http_connections=512
  fi
  set +e
  timeout --signal=TERM --kill-after=30s "$MAX_RUNTIME" \
  "$QA_PYTHON" "$TOOLS_DIR/platform_production_qa.py" \
    --mode browser-polling \
    --keep-data \
    --origin "$EXPECTED_ORIGIN" \
    --concurrency "$concurrency" \
    --http-max-connections "$browser_http_connections" \
    --browser-polling-active-users-only \
    --collect-performance \
    --report-path "$browser_report" \
    2>&1 | tee "$log_path"
  pipeline_status=("${PIPESTATUS[@]}")
  qa_status="${pipeline_status[0]}"
  tee_status="${pipeline_status[1]}"
  set -e
  if [[ "$tee_status" != "0" ]]; then
    qa_status=1
  fi
  "$SYSTEM_PYTHON" -I - "$browser_report" "$browser_root/matrix-summary.json" "$target_sha" "$run_id" "$control_email" "$qa_status" <<'PY'
import json
from pathlib import Path
import sys

report_path, summary_path, target_sha, run_id, control_email, status = sys.argv[1:]
try:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
except (OSError, ValueError):
    report = {}
marker = str(report.get("marker") or "")
user_ids = report.get("user_ids") if isinstance(report.get("user_ids"), list) else []
tournament_ids = report.get("tournament_ids") if isinstance(report.get("tournament_ids"), list) else []
passed = int(status) == 0 and report.get("passed") is True
performance = report.get("performance") if isinstance(report.get("performance"), dict) else {}
http_client = performance.get("http_client") if isinstance(performance.get("http_client"), dict) else {}
http_overall = http_client.get("overall") if isinstance(http_client.get("overall"), dict) else {}
bottleneck = performance.get("bottleneck_summary") if isinstance(performance.get("bottleneck_summary"), dict) else {}
polling = report.get("polling") if isinstance(report.get("polling"), dict) else {}
summary = {
    "mode": "browser-polling",
    "target_sha": target_sha,
    "github_run_id": int(run_id),
    "control_email": control_email.strip().lower(),
    "planned_tournaments": 20,
    "completed_tournaments": len(tournament_ids),
    "planned_users": 10000,
    "completed_users": len(user_ids),
    "passed": passed,
    "polling": {
        "profile": polling.get("profile"),
        "tabs_planned": polling.get("tabs_planned"),
        "visible_tabs": polling.get("visible_tabs"),
        "hidden_tabs": polling.get("hidden_tabs"),
        "executed": polling.get("executed"),
        "not_modified": polling.get("not_modified"),
        "deduped": polling.get("deduped"),
    },
    "performance_summary": {
        "worst_http_p95_ms": http_overall.get("p95_ms"),
        "worst_http_p99_ms": http_overall.get("p99_ms"),
        "bottleneck_classes": bottleneck.get("likely_bottleneck_classes", []),
        "resource_flags": bottleneck.get("resource_flags", {}),
    },
    "rows": [{
        "synthetic_users": len(user_ids),
        "report_path": report_path,
        "result": {
            "passed": passed,
            "marker": marker,
            "report_path": report_path,
        },
    }],
}
Path(summary_path).write_text(
    json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
PY
fi

shopt -s nullglob
summaries=("$run_root"/*/matrix-summary.json)
shopt -u nullglob
if (( ${#summaries[@]} != 1 )); then
  summary_path="$run_root/matrix-summary.json"
  "$SYSTEM_PYTHON" -I - "$summary_path" "$target_sha" "$run_id" "$qa_status" <<'PY'
import json
from pathlib import Path
import sys

path, target_sha, run_id, status = sys.argv[1:]
Path(path).write_text(
    json.dumps(
        {
            "passed": False,
            "error": "production_retained_load_matrix_summary_missing_or_ambiguous",
            "target_sha": target_sha,
            "github_run_id": run_id,
            "exit_code": int(status),
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
else
  summary_path="${summaries[0]}"
fi

# Reports are retained on the server and are intentionally private to root
# until the compact summary/log export has been copied to the workflow caller.
find "$run_root" -xdev -type f -exec chmod 0600 -- {} +

install -o "$export_uid" -g "$export_gid" -m 0600 "$summary_path" "$export_dir/matrix-summary.json"
install -o "$export_uid" -g "$export_gid" -m 0600 "$log_path" "$export_dir/matrix.log"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_EXPORT=%s\n' "$export_dir"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_SUMMARY=%s\n' "$export_dir/matrix-summary.json"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_RUN_ROOT=%s\n' "$run_root"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_EXIT_CODE=%s\n' "$qa_status"
exit "$qa_status"
