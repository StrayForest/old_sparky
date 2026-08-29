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
EXTERNAL_CONFIRMATION="RUN-PRODUCTION-EXTERNAL-LOAD"
LOCK_PATH="/run/lock/oldsparky-retained-load-matrix.lock"
EXPECTED_ORIGIN="https://old-sparky.com"
MAX_RUNTIME="180m"
HTTP_MAX_CONNECTIONS="${PLATFORM_QA_HTTP_MAX_CONNECTIONS:-512}"

if [[ ! "$HTTP_MAX_CONNECTIONS" =~ ^[1-9][0-9]{0,4}$ ]] || (( HTTP_MAX_CONNECTIONS > 10000 )); then
  echo "PLATFORM_QA_HTTP_MAX_CONNECTIONS must be an integer from 1 to 10000." >&2
  exit 1
fi

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
if (( $# < 5 || $# > 9 )) || {
  [[ "$1" != "$CONFIRMATION" ]] &&
  ! { [[ "${6:-}" == "external-vote" && "$1" == "$EXTERNAL_CONFIRMATION" ]]; }
}; then
  echo "Usage: $0 $CONFIRMATION <target-sha> <control-email> <concurrency> <run-id> [matrix|read-mix|write-burst|external-vote] [profile arguments]" >&2
  exit 2
fi

confirmation="$1"
target_sha="$2"
control_email="$3"
concurrency="$4"
run_id="$5"
profile="${6:-matrix}"
write_burst_profile=all
write_burst_users_per_tournament=50
write_burst_time_scale=1.0
external_vote_tournament_count=1
external_vote_users_per_tournament=500

case "$profile" in
  matrix) ;;
  read-mix) ;;
  write-burst)
    write_burst_profile="${7:-all}"
    write_burst_users_per_tournament="${8:-50}"
    write_burst_time_scale="${9:-1.0}"
    [[ "$write_burst_profile" == "all" || "$write_burst_profile" == "single-join" || "$write_burst_profile" == "single-ready" || "$write_burst_profile" == "multi-staggered" ]] || {
      echo "Write-burst profile must be all, single-join, single-ready or multi-staggered." >&2
      exit 1
    }
    [[ "$write_burst_users_per_tournament" =~ ^[1-9][0-9]{1,2}$ ]] && (( write_burst_users_per_tournament >= 14 && write_burst_users_per_tournament <= 500 )) || {
      echo "Write-burst users per tournament must be between 14 and 500." >&2
      exit 1
    }
    [[ "$write_burst_time_scale" =~ ^[0-9]+([.][0-9]+)?$ ]] && (( $(awk "BEGIN {print ($write_burst_time_scale >= 0.01 && $write_burst_time_scale <= 10)}") == 1 )) || {
      echo "Write-burst time scale must be between 0.01 and 10." >&2
      exit 1
    }
    ;;
  external-vote)
    [[ "$confirmation" == "$EXTERNAL_CONFIRMATION" ]] || {
      echo "External-vote profile requires the dedicated external-load confirmation." >&2
      exit 1
    }
    external_vote_tournament_count="${7:-1}"
    external_vote_users_per_tournament="${8:-500}"
    [[ "$external_vote_tournament_count" =~ ^[1-9][0-9]?$ ]] && (( external_vote_tournament_count <= 20 )) || {
      echo "External vote tournament count must be between 1 and 20." >&2
      exit 1
    }
    [[ "$external_vote_users_per_tournament" =~ ^[1-9][0-9]{1,2}$ ]] && (( external_vote_users_per_tournament >= 14 && external_vote_users_per_tournament <= 500 )) || {
      echo "External vote users per tournament must be between 14 and 500." >&2
      exit 1
    }
    ;;
  *)
    echo "Profile must be matrix, read-mix, write-burst or external-vote." >&2
    exit 1
    ;;
esac

if [[ "$profile" == "external-vote" ]]; then
  [[ "$confirmation" == "$EXTERNAL_CONFIRMATION" ]]
else
  [[ "$confirmation" == "$CONFIRMATION" ]]
fi
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
server_observability_log="$run_root/server-observability.log"

start_server_observer() {
  local target_pid="$1"
  local started_at
  started_at="$(date --iso-8601=seconds)"
  (
    set +e
    service_main_pid() {
      systemctl show --property=MainPID --value "$1" 2>/dev/null
    }

    service_control_group() {
      systemctl show --property=ControlGroup --value "$1" 2>/dev/null
    }

    print_process_resource() {
      local label="$1"
      local pid="$2"
      [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 0
      [[ -d "/proc/$pid" ]] || return 0
      local rss_kb pss_kb fd_count vm_rss vm_peak threads
      rss_kb="$(ps -p "$pid" -o rss= 2>/dev/null | awk '{print $1}')"
      fd_count="$(find "/proc/$pid/fd" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l)"
      vm_rss="$(awk '/^VmRSS:/ {print $2 " " $3}' "/proc/$pid/status" 2>/dev/null)"
      vm_peak="$(awk '/^VmPeak:/ {print $2 " " $3}' "/proc/$pid/status" 2>/dev/null)"
      pss_kb="$(awk '/^Pss:/ {print $2 " " $3}' "/proc/$pid/smaps_rollup" 2>/dev/null)"
      threads="$(awk '/^Threads:/ {print $2}' "/proc/$pid/status" 2>/dev/null)"
      echo "$label pid=$pid rss_kb=${rss_kb:-0} pss=${pss_kb:-unknown} fd_count=${fd_count:-0} vm_rss=${vm_rss:-unknown} vm_peak=${vm_peak:-unknown} threads=${threads:-0}"
      ps -p "$pid" -o pid=,ppid=,stat=,pcpu=,pmem=,rss=,etime=,comm=,args= 2>/dev/null || true
    }

    print_cgroup_resource() {
      local unit="$1"
      local group_path
      group_path="$(service_control_group "$unit")"
      [[ "$group_path" == /* ]] || return 0
      echo "cgroup unit=$unit path=$group_path"
      for metric in memory.current memory.max memory.peak memory.events pids.current pids.max; do
        if [[ -r "/sys/fs/cgroup$group_path/$metric" ]]; then
          echo "cgroup_metric unit=$unit metric=$metric value=$(tr '\n' ';' < "/sys/fs/cgroup$group_path/$metric")"
        fi
      done
    }

    load_generator_pid() {
      local parent_pid="$1"
      local candidate cmdline
      for candidate in $(pgrep -P "$parent_pid" 2>/dev/null || true); do
        [[ -r "/proc/$candidate/cmdline" ]] || continue
        cmdline="$(tr '\0' ' ' < "/proc/$candidate/cmdline" 2>/dev/null || true)"
        case "$cmdline" in
          *platform_production_qa.py*|*platform_seed_retained_tournament_matrix.py*)
            echo "$candidate"
            return 0
            ;;
        esac
      done
      echo "$parent_pid"
    }

    echo "observer_started_at=$started_at"
    echo "observer_target_pid=$target_pid"
    while kill -0 "$target_pid" 2>/dev/null; do
      echo "=== snapshot $(date --iso-8601=seconds) ==="
      echo "--- process ---"
      ps -eo pid=,ppid=,stat=,pcpu=,pmem=,rss=,comm=,args= --sort=-pcpu | head -n 32
      echo "--- process resources ---"
      load_pid="$(load_generator_pid "$target_pid")"
      print_process_resource "load_generator" "$load_pid"
      api_pid="$(service_main_pid deadlock-api)"
      worker_pid="$(service_main_pid deadlock-worker)"
      nginx_pid="$(service_main_pid nginx)"
      print_process_resource "deadlock_api_main" "$api_pid"
      print_process_resource "deadlock_worker_main" "$worker_pid"
      print_process_resource "nginx_main" "$nginx_pid"
      for child_pid in $(pgrep -P "$api_pid" 2>/dev/null); do
        print_process_resource "deadlock_api_child" "$child_pid"
      done
      for child_pid in $(pgrep -P "$nginx_pid" 2>/dev/null); do
        print_process_resource "nginx_child" "$child_pid"
      done
      print_cgroup_resource deadlock-api
      print_cgroup_resource deadlock-worker
      print_cgroup_resource nginx
      echo "--- sockets ---"
      ss -s 2>&1 || true
      ss -Htan 2>/dev/null | awk '{print $1, $4, $5}' | sort | uniq -c | sort -nr | head -n 80
      echo "--- listening queues ---"
      ss -ltn 2>&1 || true
      echo "--- kernel socket counters ---"
      for kernel_stat in /proc/net/sockstat /proc/net/sockstat6 /proc/net/netstat /proc/net/snmp; do
        if [[ -r "$kernel_stat" ]]; then
          echo "kernel_stat=$kernel_stat"
          cat "$kernel_stat"
        fi
      done
      echo "--- redis ---"
      redis-cli -h 127.0.0.1 -p 6379 info memory clients stats commandstats 2>&1 | grep -E '^(used_memory_human|maxmemory_human|used_memory_peak_human|mem_fragmentation_ratio|connected_clients|blocked_clients|tracking_clients|rejected_connections|total_connections_received|instantaneous_ops_per_sec|latest_fork_usec|cmdstat_|latency_percentiles_usec)' || true
      redis-cli -h 127.0.0.1 -p 6379 info latency 2>&1 | head -n 80 || true
      echo "--- postgres activity ---"
      timeout 5s sudo -n -u postgres psql -X -qAt -d platformdb -F '|' -c \
        "SELECT pid,state,COALESCE(wait_event_type,''),COALESCE(wait_event,''),round(EXTRACT(EPOCH FROM now()-query_start)*1000,1),left(regexp_replace(query,E'\\s+',' ','g'),240) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state<>'idle' ORDER BY query_start NULLS LAST LIMIT 16" \
        2>&1 || true
      timeout 5s sudo -n -u postgres psql -X -qAt -d platformdb -F '|' -c \
        "SELECT state,COALESCE(wait_event_type,''),COALESCE(wait_event,''),count(*) FROM pg_stat_activity WHERE datname=current_database() GROUP BY state,wait_event_type,wait_event ORDER BY count(*) DESC" \
        2>&1 || true
      sleep 5
    done
    echo "=== post-run logs $(date --iso-8601=seconds) ==="
    echo "--- api/worker journal ---"
    journalctl -u deadlock-api -u deadlock-worker --since "$started_at" --no-pager -o short-iso -n 4000 2>&1
    echo "--- nginx access ---"
    tail -n 4000 /var/log/nginx/platform-access.log 2>&1 || true
    echo "--- nginx error ---"
    tail -n 2000 /var/log/nginx/platform-error.log 2>&1 || true
  ) > "$server_observability_log" 2>&1 &
  SERVER_OBSERVER_PID="$!"
}

run_monitored() {
  local output_log="$1"
  shift
  local raw_log="$run_root/qa-command.log"
  timeout --signal=TERM --kill-after=30s "$MAX_RUNTIME" "$@" > "$raw_log" 2>&1 &
  local qa_pid="$!"
  local observer_pid
  start_server_observer "$qa_pid"
  observer_pid="$SERVER_OBSERVER_PID"
  local qa_status=0
  wait "$qa_pid" || qa_status="$?"
  wait "$observer_pid" 2>/dev/null || true
  cat "$raw_log" | tee "$output_log"
  return "$qa_status"
}

if [[ "$profile" == "matrix" ]]; then
  set +e
  run_monitored "$log_path" \
  "$QA_PYTHON" "$TOOLS_DIR/platform_seed_retained_tournament_matrix.py" \
    --env-file "$RUNTIME_ROOT/shared/.env.platform" \
    --origin "$EXPECTED_ORIGIN" \
    --control-email "$control_email" \
    --concurrency "$concurrency" \
    --output-root "$run_root"
  qa_status="$?"
  set -e
elif [[ "$profile" == "read-mix" ]]; then
  read_mix_root="$run_root/read-mix"
  install -d -o root -g root -m 0700 "$read_mix_root"
  read_mix_report="$read_mix_root/read-mix.json"
  read_mix_summary="$read_mix_root/matrix-summary.json"
  read_mix_users=10000
  read_mix_concurrency="$concurrency"
  set +e
  run_monitored "$log_path" \
  "$QA_PYTHON" "$TOOLS_DIR/platform_production_qa.py" \
    --env-file "$RUNTIME_ROOT/shared/.env.platform" \
    --mode read-mix \
    --keep-data \
    --origin "$EXPECTED_ORIGIN" \
    --scale-users "$read_mix_users" \
    --scale-site-mix-users "$read_mix_users" \
    --scale-bracket-view-users "$read_mix_users" \
    --scale-teams 2 \
    --concurrency "$read_mix_concurrency" \
    --http-max-connections "$HTTP_MAX_CONNECTIONS" \
    --http-timeout 30 \
    --collect-performance \
    --report-path "$read_mix_report"
  qa_status="$?"
  set -e
  "$SYSTEM_PYTHON" -I - "$read_mix_report" "$read_mix_summary" "$target_sha" "$run_id" "$control_email" "$qa_status" <<'PY'
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
read_mix = report.get("read_mix") if isinstance(report.get("read_mix"), dict) else {}
summary = {
    "mode": "read-mix",
    "target_sha": target_sha,
    "github_run_id": int(run_id),
    "control_email": control_email.strip().lower(),
    "planned_tournaments": 1,
    "completed_tournaments": len(tournament_ids),
    "planned_users": int(report.get("requested_users") or 0),
    "completed_users": len(user_ids),
    "passed": passed,
    "read_mix": read_mix,
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
elif [[ "$profile" == "write-burst" ]]; then
  write_burst_root="$run_root/write-burst"
  install -d -o root -g root -m 0700 "$write_burst_root"
  write_burst_report="$write_burst_root/write-burst.json"
  write_burst_summary="$write_burst_root/matrix-summary.json"
  write_burst_setup_concurrency=20
  set +e
  run_monitored "$log_path" \
  "$QA_PYTHON" "$TOOLS_DIR/platform_production_qa.py" \
    --env-file "$RUNTIME_ROOT/shared/.env.platform" \
    --mode write-burst \
    --keep-data \
    --origin "$EXPECTED_ORIGIN" \
    --concurrency "$write_burst_setup_concurrency" \
    --http-max-connections "$HTTP_MAX_CONNECTIONS" \
    --http-timeout 10 \
    --write-burst-profile "$write_burst_profile" \
    --write-burst-users-per-tournament "$write_burst_users_per_tournament" \
    --write-burst-time-scale "$write_burst_time_scale" \
    --collect-performance \
    --report-path "$write_burst_report"
  qa_status="$?"
  set -e
  "$SYSTEM_PYTHON" -I - "$write_burst_report" "$write_burst_summary" "$target_sha" "$run_id" "$control_email" "$qa_status" <<'PY'
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
write_burst = report.get("write_burst") if isinstance(report.get("write_burst"), dict) else {}
selection = str(write_burst.get("selection") or "all")
planned_tournaments = {
    "all": 26,
    "single-join": 3,
    "single-ready": 3,
    "multi-staggered": 20,
}.get(selection, 0)
planned_users = int(report.get("requested_users") or 0)
passed = int(status) == 0 and report.get("passed") is True
performance = report.get("performance") if isinstance(report.get("performance"), dict) else {}
http_client = performance.get("http_client") if isinstance(performance.get("http_client"), dict) else {}
http_overall = http_client.get("overall") if isinstance(http_client.get("overall"), dict) else {}
bottleneck = performance.get("bottleneck_summary") if isinstance(performance.get("bottleneck_summary"), dict) else {}
summary = {
    "mode": "write-burst",
    "target_sha": target_sha,
    "github_run_id": int(run_id),
    "control_email": control_email.strip().lower(),
    "planned_tournaments": planned_tournaments,
    "completed_tournaments": len(tournament_ids),
    "planned_users": planned_users,
    "completed_users": len(user_ids),
    "passed": passed,
    "write_burst": {
        "profile": write_burst.get("profile"),
        "selection": selection,
        "users_per_tournament": write_burst.get("users_per_tournament"),
        "time_scale": write_burst.get("time_scale"),
        "profiles": [
            {
                "name": row.get("name"),
                "mutations": row.get("mutations"),
                "p95_ms": (row.get("http") or {}).get("overall", {}).get("p95_ms"),
                "p99_ms": (row.get("http") or {}).get("overall", {}).get("p99_ms"),
            }
            for row in write_burst.get("profiles") or []
            if isinstance(row, dict)
        ],
        "acceptance": write_burst.get("acceptance"),
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
elif [[ "$profile" == "external-vote" ]]; then
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
    # The manifest is temporary session credential material.  It is exported
    # only to the SSH caller's private directory and is never in the report or
    # the Actions artifact.
    install -o "$export_uid" -g "$export_gid" -m 0600 \
      "$external_vote_manifest" "$export_dir/manifest.json"
    rm -f -- "$external_vote_complete"
    rm -f -- "$external_vote_ready"

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
    wait "$observer_pid" 2>/dev/null || observer_status="$?"
    observer_status="${observer_status:-0}"
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
# The exact-run root is root-owned 0700, and hardlinks are excluded so a
# report cannot change permissions on an inode outside this retained fixture.
find "$run_root" -xdev -type f -links 1 \
  -exec chown root:root -- {} + \
  -exec chmod 0600 -- {} +

install -o "$export_uid" -g "$export_gid" -m 0600 "$summary_path" "$export_dir/matrix-summary.json"
install -o "$export_uid" -g "$export_gid" -m 0600 "$log_path" "$export_dir/matrix.log"
if [[ -s "$run_root/qa-command.log" ]]; then
  install -o "$export_uid" -g "$export_gid" -m 0600 \
    "$run_root/qa-command.log" "$export_dir/qa-command.log"
fi
if [[ -s "$server_observability_log" ]]; then
  install -o "$export_uid" -g "$export_gid" -m 0600 \
    "$server_observability_log" "$export_dir/server-observability.log"
fi
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_EXPORT=%s\n' "$export_dir"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_SUMMARY=%s\n' "$export_dir/matrix-summary.json"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_RUN_ROOT=%s\n' "$run_root"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_EXIT_CODE=%s\n' "$qa_status"
exit "$qa_status"
