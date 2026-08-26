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
if (( $# < 5 || $# > 20 )) || [[ "$1" != "$CONFIRMATION" ]]; then
  echo "Usage: $0 $CONFIRMATION <target-sha> <control-email> <concurrency> <run-id> [matrix|browser-polling|sse|combined] [sse-connections sse-duration sse-open-concurrency sse-open-timeout sse-open-rate sse-capacity-limit sse-reconnect-cycles sse-users-per-tournament sse-event-count sse-event-interval combined-polling-duration combined-polling-open-stagger [public|origin-local] [ticket|legacy]]" >&2
  exit 2
fi

confirmation="$1"
target_sha="$2"
control_email="$3"
concurrency="$4"
run_id="$5"
profile="${6:-matrix}"
# Keep optional SSE controls initialized even when a release-side supervisor
# receives an older positional invocation.  The script runs with nounset and
# must still export a truthful summary rather than fail after the workload.
sse_open_rate=0
sse_capacity_limit=0
sse_reconnect_cycles=0
sse_users_per_tournament=500
sse_event_count=3
sse_event_interval=1
combined_polling_duration=30
combined_polling_open_stagger=300
sse_origin_mode=public
sse_admission_mode=ticket

case "$profile" in
  matrix|browser-polling) ;;
  sse|combined)
    sse_connections="${7:-128}"
    sse_duration="${8:-60}"
    sse_open_concurrency="${9:-256}"
    sse_open_timeout="${10:-5}"
    sse_open_rate="${11:-0}"
    sse_capacity_limit="${12:-0}"
    sse_reconnect_cycles="${13:-0}"
    sse_users_per_tournament="${14:-500}"
    sse_event_count="${15:-3}"
    sse_event_interval="${16:-1}"
    combined_polling_duration="${17:-30}"
    combined_polling_open_stagger="${18:-300}"
    sse_origin_mode="${19:-public}"
    sse_admission_mode="${20:-ticket}"
    [[ "$sse_admission_mode" == "ticket" || "$sse_admission_mode" == "legacy" ]] || {
      echo "SSE admission mode must be ticket or legacy." >&2
      exit 1
    }
    [[ "$sse_origin_mode" == "public" || "$sse_origin_mode" == "origin-local" ]] || {
      echo "SSE origin mode must be public or origin-local." >&2
      exit 1
    }
    if [[ "$sse_origin_mode" == "origin-local" && "$profile" != "sse" ]]; then
      echo "origin-local is only supported for the SSE-only profile." >&2
      exit 1
    fi
    [[ "$sse_connections" =~ ^[1-9][0-9]{0,4}$ ]] && (( sse_connections <= 30000 )) || {
      echo "SSE connections must be an integer from 1 to 30000." >&2
      exit 1
    }
    [[ "$sse_duration" =~ ^[0-9]+([.][0-9]+)?$ ]] && (( $(awk "BEGIN {print ($sse_duration >= 1 && $sse_duration <= 600)}") == 1 )) || {
      echo "SSE duration must be between 1 and 600 seconds." >&2
      exit 1
    }
    [[ "$sse_open_concurrency" =~ ^[1-9][0-9]{0,4}$ ]] && (( sse_open_concurrency <= 30000 )) || {
      echo "SSE open concurrency must be an integer from 1 to 30000." >&2
      exit 1
    }
    # Public 60s is a diagnostic ceiling only; the browser's own fallback
    # policy remains short and is not changed by this retained-load input.
    sse_open_timeout_max=60
    [[ "$sse_open_timeout" =~ ^[0-9]+([.][0-9]+)?$ ]] && (( $(awk "BEGIN {print ($sse_open_timeout >= 0.5 && $sse_open_timeout <= $sse_open_timeout_max)}") == 1 )) || {
      echo "SSE open timeout must be between 0.5 and $sse_open_timeout_max seconds for this origin mode." >&2
      exit 1
    }
    [[ "$sse_open_rate" =~ ^[0-9]+([.][0-9]+)?$ ]] && (( $(awk "BEGIN {print ($sse_open_rate >= 0 && $sse_open_rate <= 1000)}") == 1 )) || {
      echo "SSE open rate must be between 0 and 1000 new SSE/sec." >&2
      exit 1
    }
    [[ "$sse_capacity_limit" =~ ^[0-9]+$ ]] && (( sse_capacity_limit <= 30000 )) || {
      echo "SSE capacity limit must be an integer from 0 to 30000." >&2
      exit 1
    }
    if (( sse_capacity_limit > 3000 )) && {
      [[ "$profile" != "sse" || "$sse_origin_mode" != "origin-local" || "$sse_admission_mode" != "ticket" ]]
    }; then
      echo "High SSE capacity mode requires the ticketed SSE-only loopback origin." >&2
      exit 1
    fi
    [[ "$sse_reconnect_cycles" =~ ^[0-9]{1,2}$ ]] && (( sse_reconnect_cycles <= 10 )) || {
      echo "SSE reconnect cycles must be an integer from 0 to 10." >&2
      exit 1
    }
    [[ "$sse_users_per_tournament" =~ ^[1-9][0-9]{0,2}$ ]] && (( sse_users_per_tournament >= 10 && sse_users_per_tournament <= 500 )) || {
      echo "SSE users per tournament must be between 10 and 500." >&2
      exit 1
    }
    [[ "$sse_event_count" =~ ^[0-9]{1,3}$ ]] && (( sse_event_count <= 100 )) || {
      echo "SSE event count must be an integer from 0 to 100." >&2
      exit 1
    }
    [[ "$sse_event_interval" =~ ^[0-9]+([.][0-9]+)?$ ]] && (( $(awk "BEGIN {print ($sse_event_interval >= 0 && $sse_event_interval <= 60)}") == 1 )) || {
      echo "SSE event interval must be between 0 and 60 seconds." >&2
      exit 1
    }
    [[ "$combined_polling_duration" =~ ^[0-9]+([.][0-9]+)?$ ]] && (( $(awk "BEGIN {print ($combined_polling_duration >= 1 && $combined_polling_duration <= 300)}") == 1 )) || {
      echo "Combined polling duration must be between 1 and 300 seconds." >&2
      exit 1
    }
    [[ "$combined_polling_open_stagger" =~ ^[0-9]+([.][0-9]+)?$ ]] && (( $(awk "BEGIN {print ($combined_polling_open_stagger >= 0 && $combined_polling_open_stagger <= 600)}") == 1 )) || {
      echo "Combined polling open stagger must be between 0 and 600 seconds." >&2
      exit 1
    }
    ;;
  *)
    echo "Profile must be matrix, browser-polling, sse or combined." >&2
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
      local rss_kb fd_count vm_rss vm_peak threads
      rss_kb="$(ps -p "$pid" -o rss= 2>/dev/null | awk '{print $1}')"
      fd_count="$(find "/proc/$pid/fd" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l)"
      vm_rss="$(awk '/^VmRSS:/ {print $2 " " $3}' "/proc/$pid/status" 2>/dev/null)"
      vm_peak="$(awk '/^VmPeak:/ {print $2 " " $3}' "/proc/$pid/status" 2>/dev/null)"
      threads="$(awk '/^Threads:/ {print $2}' "/proc/$pid/status" 2>/dev/null)"
      echo "$label pid=$pid rss_kb=${rss_kb:-0} fd_count=${fd_count:-0} vm_rss=${vm_rss:-unknown} vm_peak=${vm_peak:-unknown} threads=${threads:-0}"
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

    echo "observer_started_at=$started_at"
    echo "observer_target_pid=$target_pid"
    while kill -0 "$target_pid" 2>/dev/null; do
      echo "=== snapshot $(date --iso-8601=seconds) ==="
      echo "--- process ---"
      ps -eo pid=,ppid=,stat=,pcpu=,pmem=,rss=,comm=,args= --sort=-pcpu | head -n 32
      echo "--- process resources ---"
      print_process_resource "load_generator" "$target_pid"
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
    --output-root "$run_root" \
  qa_status="$?"
  set -e
elif [[ "$profile" == "browser-polling" ]]; then
  browser_root="$run_root/browser-polling"
  install -d -o root -g root -m 0700 "$browser_root"
  browser_report="$browser_root/browser-polling.json"
  # The measured 10k profile uses a bounded client pool.  The previous
  # concurrency*4 rule opened 320 connections for the normal concurrency=80
  # dispatch and exhausted the API database pool before the VPS CPU was busy.
  browser_http_connections="$HTTP_MAX_CONNECTIONS"
  browser_setup_concurrency=20
  set +e
  run_monitored "$log_path" \
  "$QA_PYTHON" "$TOOLS_DIR/platform_production_qa.py" \
    --env-file "$RUNTIME_ROOT/shared/.env.platform" \
    --mode browser-polling \
    --keep-data \
    --origin "$EXPECTED_ORIGIN" \
    --concurrency "$browser_setup_concurrency" \
    --http-max-connections "$browser_http_connections" \
    --http-timeout 10 \
    --browser-polling-duration 30 \
    --browser-polling-open-stagger 300 \
    --collect-performance \
    --report-path "$browser_report"
  qa_status="$?"
  set -e
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
else
  # A persistent SSE probe needs one client-side descriptor per live stream
  # plus setup/API/Redis descriptors.  The default shell soft limit on some
  # production images is 1024, which makes a 1k probe fail inside the load
  # generator with Errno 24 before the origin has a chance to respond.
  # Raise only the QA child-process limit and never change the server units.
  nofile_soft="$(ulimit -Sn)"
  nofile_hard="$(ulimit -Hn)"
  if [[ "$nofile_soft" =~ ^[0-9]+$ ]]; then
    nofile_target=65535
    if [[ "$nofile_hard" =~ ^[0-9]+$ ]] && (( nofile_hard < nofile_target )); then
      nofile_target="$nofile_hard"
    fi
    if (( nofile_soft < nofile_target )); then
      ulimit -n "$nofile_target"
    fi
  fi
  echo "SSE load-generator nofile soft=$(ulimit -Sn) hard=$(ulimit -Hn)"
  sse_root="$run_root/$profile"
  install -d -o root -g root -m 0700 "$sse_root"
  sse_report="$sse_root/$profile.json"
  sse_summary="$sse_root/matrix-summary.json"
  sse_origin="$EXPECTED_ORIGIN"
  if [[ "$sse_origin_mode" == "origin-local" ]]; then
    sse_origin="http://127.0.0.1:8010"
  fi
  # Fixture creation performs authenticated CSRF/session reads. Keep that
  # setup below the API pool budget; SSE opening pressure is controlled
  # independently by --sse-open-concurrency below.
  # Keep setup concurrency bounded separately from the client connection
  # ceiling. A 40-connection pool makes a 10k browser profile measure the
  # load generator's own queue instead of the public origin. The API pool
  # remains bounded server-side; this only lets the public client contour
  # expose origin saturation when many tabs become ready together.
  sse_setup_concurrency=20
  set +e
  run_monitored "$log_path" \
  "$QA_PYTHON" "$TOOLS_DIR/platform_sse_qa.py" \
    --env-file "$RUNTIME_ROOT/shared/.env.platform" \
    --origin "$sse_origin" \
    --request-origin "$EXPECTED_ORIGIN" \
    --mode "$profile" \
    --control-email "$control_email" \
    --target-sha "$target_sha" \
    --github-run-id "$run_id" \
    --users-per-tournament "$sse_users_per_tournament" \
    --sse-connections "$sse_connections" \
    --sse-duration "$sse_duration" \
    --sse-open-concurrency "$sse_open_concurrency" \
    --sse-open-timeout "$sse_open_timeout" \
    --sse-open-rate "$sse_open_rate" \
    --sse-capacity-limit "$sse_capacity_limit" \
    --sse-reconnect-cycles "$sse_reconnect_cycles" \
    --sse-event-count "$sse_event_count" \
    --sse-event-interval "$sse_event_interval" \
    --combined-polling-duration "$combined_polling_duration" \
    --combined-polling-open-stagger "$combined_polling_open_stagger" \
    --sse-admission-mode "$sse_admission_mode" \
    --concurrency "$sse_setup_concurrency" \
    --http-max-connections "$HTTP_MAX_CONNECTIONS" \
    --http-timeout 10 \
    --report-path "$sse_report" \
    --summary-path "$sse_summary" \
    --keep-data
  qa_status="$?"
  set -e
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
