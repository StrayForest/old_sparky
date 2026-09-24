#!/usr/bin/env bash
set +x
set -euo pipefail

# This supervisor owns only the origin-side fixture lifecycle for the external
# production load workflow.  The measured HTTP generator runs on the
# GitHub-hosted runner; no production retained-matrix generator is allowed
# here.
RUNTIME_ROOT="/opt/oldsparky/platform"
PLATFORM_ROOT="$RUNTIME_ROOT/current"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$(readlink -f -- "$TOOLS_DIR/platform_production_external_fixture_qa.sh")"
QA_PYTHON="$RUNTIME_ROOT/shared/venv/bin/python"
OUTPUT_ROOT_BASE="$RUNTIME_ROOT/shared/production-retained-matrix"
SYSTEM_PYTHON="/usr/bin/python3.12"
EXTERNAL_CONFIRMATION="RUN-PRODUCTION-EXTERNAL-LOAD"
TIMEOUT_DIAGNOSTICS_CONFIRMATION="RUN-PRODUCTION-TIMEOUT-DIAGNOSTICS"
EXPECTED_ORIGIN="https://old-sparky.com"
MAX_RUNTIME="180m"
# Explicit ``-B`` flags below are the primary contract.  Keep this defense
# for the venv-backed fixture and observer children as well, so no Python
# child writes into the root-owned active release.
export PYTHONDONTWRITEBYTECODE=1

if [[ "$EUID" -ne 0 ]]; then
  echo "Production external-load fixture supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Production external-load fixture must run from the active immutable release." >&2
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
platform_retained_load_lock_supervise "${ORIGINAL_ARGS[@]}" || {
  echo "Another retained load or cleanup operation is already running on this host." >&2
  exit 1
}
if [[ "${PLATFORM_RETAINED_LOAD_LOCK_SUPERVISED:-}" != "1" ]]; then
  exit 0
fi
platform_retained_load_lock_open || {
  echo "Retained-load lock supervisor could not be validated." >&2
  exit 1
}
trap platform_retained_load_lock_close EXIT
if (( $# != 8 && $# != 9 )); then
  echo "Usage: $0 $EXTERNAL_CONFIRMATION <target-sha> <control-email> <concurrency> <run-id> external-vote <tournament-count> <users-per-tournament> [timeout-path]" >&2
  exit 2
fi

confirmation="$1"
target_sha="$2"
control_email="$3"
concurrency="$4"
run_id="$5"
profile="$6"
external_vote_tournament_count="$7"
external_vote_users_per_tournament="$8"
timeout_diagnostics="${9:-false}"

[[ "$profile" == "external-vote" ]] || {
  echo "External-load fixture supports only the external-vote profile." >&2
  exit 1
}
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Target SHA must be a lowercase 40-character commit SHA." >&2
  exit 1
}
"$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_workflow_input_guard.py" email \
  --value "$control_email" || {
  echo "Control email is invalid." >&2
  exit 1
}
if [[ ! "$concurrency" =~ ^[1-9][0-9]{0,2}$ ]] || (( concurrency > 256 )); then
  echo "Concurrency must be an integer from 1 to 256." >&2
  exit 1
fi
[[ "$run_id" =~ ^[1-9][0-9]{0,31}$ ]] || {
  echo "Run id must be numeric." >&2
  exit 1
}
if [[ ! "$external_vote_tournament_count" =~ ^[1-9][0-9]?$ ]] \
  || (( external_vote_tournament_count > 40 )); then
  echo "External vote tournament count must be between 1 and 40." >&2
  exit 1
fi
if [[ ! "$external_vote_users_per_tournament" =~ ^[1-9][0-9]{1,2}$ ]] \
  || (( external_vote_users_per_tournament < 14 || external_vote_users_per_tournament > 500 )); then
  echo "External vote users per tournament must be between 14 and 500." >&2
  exit 1
fi
[[ "$timeout_diagnostics" == "true" || "$timeout_diagnostics" == "false" ]] || {
  echo "Timeout diagnostics mode must be true or false." >&2
  exit 1
}
if [[ "$timeout_diagnostics" == "true" ]]; then
  [[ "$confirmation" == "$TIMEOUT_DIAGNOSTICS_CONFIRMATION" ]] || {
    echo "Timeout diagnostics require the dedicated timeout-diagnostics confirmation." >&2
    exit 1
  }
else
  [[ "$confirmation" == "$EXTERNAL_CONFIRMATION" ]] || {
    echo "External-load fixture requires the dedicated external-load confirmation." >&2
    exit 1
  }
fi

test -x "$QA_PYTHON" || {
  echo "Production QA Python runtime is missing." >&2
  exit 1
}
test -L "$RUNTIME_ROOT/current" || {
  echo "Active production release is missing." >&2
  exit 1
}

release_sha="$($SYSTEM_PYTHON -I -B - "$RUNTIME_ROOT/current/RELEASE.json" <<'PY'
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

platform_environment="$($SYSTEM_PYTHON -I -B "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_ENVIRONMENT)"
test "$platform_environment" = "production" || {
  echo "Production external-load fixture requires PLATFORM_ENVIRONMENT=production." >&2
  exit 1
}
platform_origin="$($SYSTEM_PYTHON -I -B "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_WEB_ORIGIN)"
test "$platform_origin" = "$EXPECTED_ORIGIN" || {
  echo "Production external-load fixture requires the canonical origin." >&2
  exit 1
}

run_root="$OUTPUT_ROOT_BASE/gha-$run_id"
export_dir="/tmp/old-sparky-production-retained-load-$run_id"
supervisor_exit_path="$export_dir/supervisor.exit"
# ShellCheck cannot infer functions invoked through a dynamically registered
# EXIT trap; this callback is reachable only when that trap fires.
# shellcheck disable=SC2317
write_supervisor_exit() {
  local supervisor_status=$?
  if [[ -d "$export_dir" && ! -L "$export_dir" ]]; then
    printf '%s\n' "$supervisor_status" > "$supervisor_exit_path"
    chmod 0600 "$supervisor_exit_path"
    if [[ "${export_uid:-}" =~ ^[0-9]+$ && "${export_gid:-}" =~ ^[0-9]+$ ]]; then
      chown "$export_uid:$export_gid" "$supervisor_exit_path"
    fi
  fi
  # This trap replaces the acquisition trap above; close the retained-load
  # descriptor explicitly before returning the supervisor status.
  platform_retained_load_lock_close
  exit "$supervisor_status"
}
trap write_supervisor_exit EXIT
export_uid="${SUDO_UID:-0}"
export_gid="${SUDO_GID:-0}"
[[ "$export_uid" =~ ^[0-9]+$ && "$export_gid" =~ ^[0-9]+$ ]] || {
  echo "Unable to determine the SSH caller identity for report export." >&2
  exit 1
}
if [[ -e "$run_root" || -L "$run_root" ]]; then
  echo "A production external-load run already exists for this GitHub run id." >&2
  exit 1
fi

install -d -o root -g root -m 0700 "$OUTPUT_ROOT_BASE"
install -d -o root -g root -m 0700 "$run_root"
rm -rf -- "$export_dir"
install -d -o "$export_uid" -g "$export_gid" -m 0700 "$export_dir"
log_path="$run_root/canonical.log"
server_observability_log="$run_root/server-observability.json"
external_vote_root="$run_root/external-vote"
install -d -o root -g root -m 0700 "$external_vote_root"
external_vote_report="$external_vote_root/external-vote.json"
external_vote_manifest="$external_vote_root/manifest.json"
external_vote_summary="$external_vote_root/matrix-summary.json"
external_vote_complete="$export_dir/complete"
external_vote_ready="$export_dir/ready"
external_vote_observer_output="$external_vote_root/server-observability.json"
external_vote_observer_log="$external_vote_root/server-observer.log"
timeout_diagnostic_ids_path="$export_dir/timeout-diagnostic-ids.json"

set +e
timeout --signal=TERM --kill-after=30s "$MAX_RUNTIME" \
  env PLATFORM_RUNTIME_SERVICE=qa \
  "$QA_PYTHON" -B "$TOOLS_DIR/platform_prepare_external_vote_fixture.py" \
    --env-file "$RUNTIME_ROOT/shared/.env.platform" \
    --origin "$EXPECTED_ORIGIN" \
    --local-origin "http://127.0.0.1:8010" \
    --report-path "$external_vote_report" \
    --manifest-path "$external_vote_manifest" \
    --tournament-count "$external_vote_tournament_count" \
    --users-per-tournament "$external_vote_users_per_tournament" \
    --concurrency "$concurrency" \
    --http-timeout 30 \
    > "$run_root/qa-command.log" 2>&1
qa_status="$?"
set -e

# The command log can contain exception text, request values or credential
# material from a failed setup.  Reduce it to one bounded fixed-schema record
# before anything is exported, then remove the raw source.
"$SYSTEM_PYTHON" -I -B "$TOOLS_DIR/platform_evidence_sanitizer.py" \
  --input "$run_root/qa-command.log" --output "$log_path"
rm -f -- "$run_root/qa-command.log"
test ! -e "$run_root/qa-command.log"

"$SYSTEM_PYTHON" -I -B - "$external_vote_report" "$external_vote_summary" \
  "$qa_status" \
  "$external_vote_tournament_count" "$external_vote_users_per_tournament" <<'PY'
import json
from pathlib import Path
import sys

(
    report_path,
    summary_path,
    status,
    tournament_count,
    users_per_tournament,
) = sys.argv[1:]
try:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    report = {}
if not isinstance(report, dict):
    report = {}
marker = str(report.get("marker") or "")
user_ids = report.get("user_ids") if isinstance(report.get("user_ids"), list) else []
tournament_ids = report.get("tournament_ids") if isinstance(report.get("tournament_ids"), list) else []
passed = int(status) == 0 and report.get("passed") is True
summary = {
    "mode": "write-burst",
    "control_account_preserved": False,
    "planned_tournaments": int(tournament_count),
    "completed_tournaments": len(tournament_ids),
    "planned_users": int(tournament_count) * int(users_per_tournament),
    "completed_users": len(user_ids),
    "passed": passed,
    "external_vote_fixture": {
        "tournament_count": int(tournament_count),
        "users_per_tournament": int(users_per_tournament),
        "measurement_runs_on_external_runner": True,
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

if [[ "$qa_status" == "0" && -s "$external_vote_manifest" ]]; then
  # The manifest is temporary session credential material. It is exported
  # only to the SSH caller's private directory and is never in a report or
  # Actions artifact.
  install -o "$export_uid" -g "$export_gid" -m 0600 \
    "$external_vote_manifest" "$export_dir/manifest.json"
  rm -f -- "$external_vote_complete" "$external_vote_ready"

  # Bind the observer to the exact fixture created by this run.  The prepare
  # command writes the marker to both the manifest and report; requiring an
  # exact, independently validated match prevents a stale/shared fixture from
  # being presented as origin evidence for this external run.
  fixture_marker=""
  if ! fixture_marker="$($SYSTEM_PYTHON -I -B - "$external_vote_manifest" "$external_vote_report" <<'PY'
import json
import re
from pathlib import Path
import sys

manifest_path, report_path = (Path(value) for value in sys.argv[1:])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
report = json.loads(report_path.read_text(encoding="utf-8"))
marker = manifest.get("marker")
if not isinstance(marker, str) or re.fullmatch(r"preprod[0-9]{12}[0-9a-f]{4}", marker) is None:
    raise SystemExit("prepared fixture marker is invalid")
if report.get("marker") != marker:
    raise SystemExit("prepared fixture marker/report mismatch")
print(marker)
PY
  )"; then
    echo "Prepared fixture marker could not be validated for observer binding." >&2
    rm -f -- "$external_vote_manifest" "$export_dir/manifest.json"
    qa_status=1
  fi

  if [[ "$qa_status" == "0" && -n "$fixture_marker" ]]; then
  set +e
  observer_args=(
    "$TOOLS_DIR/platform_external_load_observer.py"
    --env-file "$RUNTIME_ROOT/shared/.env.platform"
    --output "$external_vote_observer_output"
    --stop-file "$external_vote_complete"
    --interval 1
    --max-runtime 10_800
    --fixture-marker "$fixture_marker"
    --external-run-id "$run_id"
  )
  if [[ "$timeout_diagnostics" == "true" ]]; then
    observer_args+=(--diagnostic-id-file "$timeout_diagnostic_ids_path")
  fi
  timeout --signal=TERM --kill-after=30s "$MAX_RUNTIME" \
    env PLATFORM_RUNTIME_SERVICE=observer \
    "$QA_PYTHON" -B "${observer_args[@]}" \
      > "$external_vote_observer_log" 2>&1 &
  observer_pid="$!"
  set -e
  sleep 1
  kill -0 "$observer_pid" 2>/dev/null || {
    echo "External-load observer failed to start." >&2
    qa_status=1
  }
  if [[ "$qa_status" == "0" ]]; then
    : > "$external_vote_ready"
  fi
  printf 'PRODUCTION_EXTERNAL_LOAD_READY=%s\n' "$export_dir/manifest.json"
  observer_deadline=$(( $(date +%s) + 10800 ))
  while [[ ! -e "$external_vote_complete" ]]; do
    if ! kill -0 "$observer_pid" 2>/dev/null; then
      wait "$observer_pid" 2>/dev/null || true
      echo "External-load observer exited before the load completed." >&2
      qa_status=1
      break
    fi
    if (( $(date +%s) >= observer_deadline )); then
      echo "External load completion barrier timed out." >&2
      qa_status=1
      break
    fi
    sleep 1
  done
  if [[ ! -e "$external_vote_complete" ]]; then
    : > "$external_vote_complete"
  fi
  observer_status=0
  wait "$observer_pid" 2>/dev/null || observer_status="$?"
  if [[ "$observer_status" != "0" ]]; then
    qa_status=1
  fi
  if [[ -s "$external_vote_observer_output" ]]; then
    cp "$external_vote_observer_output" "$server_observability_log"
  else
    # Preserve a debuggable fixed-schema failure without exporting the raw
    # observer stderr (which may contain paths, IDs or SQL diagnostics).
    "$SYSTEM_PYTHON" -I -B - "$server_observability_log" "$observer_status" <<'PY'
import json
from pathlib import Path
import sys

destination, status = sys.argv[1:]
Path(destination).write_text(
    json.dumps(
        {
            "schema": 1,
            "available": False,
            "error_class": "observer_unavailable",
            "observer_exit_code": int(status),
            "binding": {"complete": False},
            "system": {},
            "server_request_perf_logs": {"logged_requests": 0},
            "server_ssr_observability": {"timeout_diagnostics": {"rows": []}},
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
  fi
  # No credential-bearing manifest should survive the measurement barrier.
  rm -f -- "$external_vote_manifest" "$export_dir/manifest.json" "$export_dir/timeout-diagnostic-ids.json" "$external_vote_ready"
  test ! -e "$external_vote_manifest"
  test ! -e "$export_dir/manifest.json"
  test ! -e "$export_dir/timeout-diagnostic-ids.json"
  rm -f -- "$external_vote_observer_log"
  test ! -e "$external_vote_observer_log"
  fi
fi

shopt -s nullglob
summaries=("$run_root"/*/matrix-summary.json)
shopt -u nullglob
if (( ${#summaries[@]} != 1 )); then
  summary_path="$run_root/matrix-summary.json"
  "$SYSTEM_PYTHON" -I -B - "$summary_path" "$run_id" "$qa_status" <<'PY'
import json
from pathlib import Path
import sys

path, run_id, status = sys.argv[1:]
Path(path).write_text(
    json.dumps(
        {
            "passed": False,
            "error": "production_external_load_summary_missing_or_ambiguous",
            "github_run_id": int(run_id),
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

# Keep the exact origin-side report private until the compact export is copied.
find "$run_root" -xdev -type f -links 1 \
  -exec chown root:root -- {} + \
  -exec chmod 0600 -- {} +

"$SYSTEM_PYTHON" -I -B - "$summary_path" "$export_dir/matrix-summary.json" <<'PY'
import json
from pathlib import Path
import sys

source_path, destination = sys.argv[1:]
try:
    payload = json.loads(Path(source_path).read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    payload = {}
if not isinstance(payload, dict):
    payload = {}
rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
safe_rows = []
for row in rows[:20]:
    if not isinstance(row, dict):
        continue
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    synthetic_users = row.get("synthetic_users")
    if (
        isinstance(synthetic_users, bool)
        or not isinstance(synthetic_users, int)
        or synthetic_users < 0
        or synthetic_users > 1_000_000_000
    ):
        synthetic_users = 0
    safe_rows.append(
        {
            "synthetic_users": synthetic_users,
            "result": {
                "passed": result.get("passed") is True,
            },
        }
    )
def safe_count(key):
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000 else 0
mode = payload.get("mode")
if not isinstance(mode, str) or mode not in {"scale", "read-mix", "write-burst"}:
    mode = "other"
safe = {
    "schema": 1,
    "mode": mode,
    "control_account_preserved": payload.get("control_account_preserved") is True,
    "planned_tournaments": safe_count("planned_tournaments"),
    "completed_tournaments": safe_count("completed_tournaments"),
    "planned_users": safe_count("planned_users"),
    "completed_users": safe_count("completed_users"),
    "passed": payload.get("passed") is True,
    "rows": safe_rows,
}
Path(destination).write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
chown "$export_uid:$export_gid" "$export_dir/matrix-summary.json"
chmod 0600 "$export_dir/matrix-summary.json"
install -o "$export_uid" -g "$export_gid" -m 0600 \
  "$log_path" "$export_dir/canonical.log"
if [[ -s "$server_observability_log" ]]; then
  install -o "$export_uid" -g "$export_gid" -m 0600 \
    "$server_observability_log" "$export_dir/server-observability.json"
fi
printf 'PRODUCTION_EXTERNAL_LOAD_EXPORT=%s\n' "$export_dir"
printf 'PRODUCTION_EXTERNAL_LOAD_SUMMARY=%s\n' "$export_dir/matrix-summary.json"
printf 'PRODUCTION_EXTERNAL_LOAD_RUN_ROOT=%s\n' "$run_root"
printf 'PRODUCTION_EXTERNAL_LOAD_EXIT_CODE=%s\n' "$qa_status"
exit "$qa_status"
