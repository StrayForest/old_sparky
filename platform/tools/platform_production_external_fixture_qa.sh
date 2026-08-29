#!/usr/bin/env bash
set +x
set -euo pipefail

# This supervisor owns only the origin-side fixture lifecycle for the external
# production load workflow.  The measured HTTP generator runs on the
# GitHub-hosted runner; no production retained-matrix generator is allowed
# here.
TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_production_external_fixture_qa.sh"
QA_PYTHON="$PLATFORM_ROOT/.venv_platform/bin/python"
RUNTIME_ROOT="/opt/oldsparky/platform"
OUTPUT_ROOT_BASE="$RUNTIME_ROOT/shared/production-retained-matrix"
SYSTEM_PYTHON="/usr/bin/python3.12"
EXTERNAL_CONFIRMATION="RUN-PRODUCTION-EXTERNAL-LOAD"
LOCK_PATH="/run/lock/oldsparky-retained-load-matrix.lock"
EXPECTED_ORIGIN="https://old-sparky.com"
MAX_RUNTIME="180m"

if [[ "$EUID" -ne 0 ]]; then
  echo "Production external-load fixture supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Production external-load fixture must run from the fixed root-controlled checkout." >&2
  exit 1
fi
exec 9>"$LOCK_PATH"
flock -n 9 || {
  echo "Another retained load or cleanup operation is already running on this host." >&2
  exit 1
}
if (( $# != 8 )); then
  echo "Usage: $0 $EXTERNAL_CONFIRMATION <target-sha> <control-email> <concurrency> <run-id> external-vote <tournament-count> <users-per-tournament>" >&2
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

[[ "$confirmation" == "$EXTERNAL_CONFIRMATION" ]] || {
  echo "External-load fixture requires the dedicated external-load confirmation." >&2
  exit 1
}
[[ "$profile" == "external-vote" ]] || {
  echo "External-load fixture supports only the external-vote profile." >&2
  exit 1
}
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
[[ "$external_vote_tournament_count" =~ ^[1-9][0-9]?$ ]] && (( external_vote_tournament_count <= 20 )) || {
  echo "External vote tournament count must be between 1 and 20." >&2
  exit 1
}
[[ "$external_vote_users_per_tournament" =~ ^[1-9][0-9]{1,2}$ ]] && (( external_vote_users_per_tournament >= 14 && external_vote_users_per_tournament <= 500 )) || {
  echo "External vote users per tournament must be between 14 and 500." >&2
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
  echo "Production external-load fixture requires PLATFORM_ENVIRONMENT=production." >&2
  exit 1
}
platform_origin="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_WEB_ORIGIN)"
test "$platform_origin" = "$EXPECTED_ORIGIN" || {
  echo "Production external-load fixture requires the canonical origin." >&2
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
  echo "A production external-load run already exists for this GitHub run id." >&2
  exit 1
fi

install -d -o root -g root -m 0700 "$OUTPUT_ROOT_BASE"
install -d -o root -g root -m 0700 "$run_root"
rm -rf -- "$export_dir"
install -d -o "$export_uid" -g "$export_gid" -m 0700 "$export_dir"
log_path="$run_root/matrix.log"
server_observability_log="$run_root/server-observability.log"
external_vote_root="$run_root/external-vote"
install -d -o root -g root -m 0700 "$external_vote_root"
external_vote_report="$external_vote_root/external-vote.json"
external_vote_manifest="$external_vote_root/manifest.json"
external_vote_summary="$external_vote_root/matrix-summary.json"
external_vote_complete="$export_dir/complete"
external_vote_ready="$export_dir/ready"
external_vote_observer_output="$external_vote_root/server-observability.json"
external_vote_observer_log="$external_vote_root/server-observer.log"

set +e
timeout --signal=TERM --kill-after=30s "$MAX_RUNTIME" \
  "$QA_PYTHON" "$TOOLS_DIR/platform_prepare_external_vote_fixture.py" \
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
cp "$run_root/qa-command.log" "$log_path"

"$SYSTEM_PYTHON" -I - "$external_vote_report" "$external_vote_summary" \
  "$target_sha" "$run_id" "$control_email" "$qa_status" \
  "$external_vote_tournament_count" "$external_vote_users_per_tournament" <<'PY'
import json
from pathlib import Path
import sys

(
    report_path,
    summary_path,
    target_sha,
    run_id,
    control_email,
    status,
    tournament_count,
    users_per_tournament,
) = sys.argv[1:]
try:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    report = {}
marker = str(report.get("marker") or "")
user_ids = report.get("user_ids") if isinstance(report.get("user_ids"), list) else []
tournament_ids = report.get("tournament_ids") if isinstance(report.get("tournament_ids"), list) else []
passed = int(status) == 0 and report.get("passed") is True
summary = {
    "mode": "write-burst",
    "target_sha": target_sha,
    "github_run_id": int(run_id),
    "control_email": control_email.strip().lower(),
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
            "tournament_slug": (report.get("tournament_slugs") or [None])[0],
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

  set +e
  timeout --signal=TERM --kill-after=30s "$MAX_RUNTIME" \
    "$QA_PYTHON" "$TOOLS_DIR/platform_external_load_observer.py" \
      --env-file "$RUNTIME_ROOT/shared/.env.platform" \
      --output "$external_vote_observer_output" \
      --stop-file "$external_vote_complete" \
      --interval 1 \
      --max-runtime 10_800 \
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
    cp "$external_vote_observer_log" "$server_observability_log"
  fi
  # No credential-bearing manifest should survive the measurement barrier.
  rm -f -- "$external_vote_manifest" "$export_dir/manifest.json" "$external_vote_ready"
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
            "error": "production_external_load_summary_missing_or_ambiguous",
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

# Keep the exact origin-side report private until the compact export is copied.
find "$run_root" -xdev -type f -links 1 \
  -exec chown root:root -- {} + \
  -exec chmod 0600 -- {} +

install -o "$export_uid" -g "$export_gid" -m 0600 \
  "$summary_path" "$export_dir/matrix-summary.json"
install -o "$export_uid" -g "$export_gid" -m 0600 \
  "$log_path" "$export_dir/matrix.log"
if [[ -s "$run_root/qa-command.log" ]]; then
  install -o "$export_uid" -g "$export_gid" -m 0600 \
    "$run_root/qa-command.log" "$export_dir/qa-command.log"
fi
if [[ -s "$server_observability_log" ]]; then
  install -o "$export_uid" -g "$export_gid" -m 0600 \
    "$server_observability_log" "$export_dir/server-observability.log"
fi
printf 'PRODUCTION_EXTERNAL_LOAD_EXPORT=%s\n' "$export_dir"
printf 'PRODUCTION_EXTERNAL_LOAD_SUMMARY=%s\n' "$export_dir/matrix-summary.json"
printf 'PRODUCTION_EXTERNAL_LOAD_RUN_ROOT=%s\n' "$run_root"
printf 'PRODUCTION_EXTERNAL_LOAD_EXIT_CODE=%s\n' "$qa_status"
exit "$qa_status"
