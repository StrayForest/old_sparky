#!/usr/bin/env bash
set +x
set -Eeuo pipefail

TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_production_sse_failure_recovery_qa.sh"
QA_PYTHON="$PLATFORM_ROOT/.venv_platform/bin/python"
RUNTIME_ROOT="/opt/oldsparky/platform"
RUN_ROOT_BASE="$RUNTIME_ROOT/shared/production-retained-matrix"
SYSTEM_PYTHON="/usr/bin/python3.12"
CONFIRMATION="RUN-PRODUCTION-SSE-FAILURE-RECOVERY"
LOCK_PATH="/run/lock/oldsparky-retained-load-matrix.lock"
EXPECTED_ORIGIN="https://old-sparky.com"
MAX_RUNTIME="240s"
SSE_CONNECTIONS=32
SSE_DURATION=150
SSE_OPEN_RATE=10
SSE_OPEN_TIMEOUT=5
RECOVERY_TIMEOUT=60

if [[ "$EUID" -ne 0 ]]; then
  echo "Production SSE failure/recovery supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Failure/recovery test must run from the fixed root-controlled checkout." >&2
  exit 1
fi
exec 9>"$LOCK_PATH"
flock -n 9 || {
  echo "Another retained load or cleanup operation is already running on this host." >&2
  exit 1
}
if (( $# != 5 )) || [[ "$1" != "$CONFIRMATION" ]]; then
  echo "Usage: $0 $CONFIRMATION <target-sha> <control-email> <run-id> <fault>" >&2
  exit 2
fi

confirmation="$1"
target_sha="$2"
control_email="$3"
run_id="$4"
fault="$5"
[[ "$confirmation" == "$CONFIRMATION" ]]
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ "$control_email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || exit 1
[[ "$run_id" =~ ^[0-9]+$ ]] || exit 1
case "$fault" in
  api-worker-restart|api-restart|redis-hiccup|nginx-reload|mass-disconnect) ;;
  *)
    echo "Unsupported fault: $fault" >&2
    exit 1
    ;;
esac

test -d "$TRUSTED_REPO_ROOT/.git"
test -x "$QA_PYTHON"
test -L "$RUNTIME_ROOT/current"
checkout_sha="$(git -C "$TRUSTED_REPO_ROOT" rev-parse --verify HEAD)"
test "$checkout_sha" = "$target_sha" || {
  echo "Trusted checkout does not match the dispatched target SHA." >&2
  exit 1
}
release_sha="$($SYSTEM_PYTHON -I - "$RUNTIME_ROOT/current/RELEASE.json" <<'PY'
import json
from pathlib import Path
import sys

print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("source_git_commit", ""))
PY
)"
test "$release_sha" = "$target_sha" || {
  echo "Active production release does not match the dispatched target SHA." >&2
  exit 1
}
platform_environment="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_ENVIRONMENT)"
test "$platform_environment" = production || exit 1
platform_origin="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_WEB_ORIGIN)"
test "$platform_origin" = "$EXPECTED_ORIGIN" || exit 1

run_root="$RUN_ROOT_BASE/gha-$run_id"
export_dir="/tmp/old-sparky-production-sse-recovery-$run_id"
export_uid="${SUDO_UID:-0}"
export_gid="${SUDO_GID:-0}"
[[ "$export_uid" =~ ^[0-9]+$ && "$export_gid" =~ ^[0-9]+$ ]] || exit 1
if [[ -e "$run_root" || -L "$run_root" ]]; then
  echo "A retained run already exists for this run id." >&2
  exit 1
fi
install -d -o root -g root "$RUN_ROOT_BASE" "$run_root" "$run_root/sse"
chown root:root "$RUN_ROOT_BASE" "$run_root" "$run_root/sse"
chmod 0700 "$RUN_ROOT_BASE" "$run_root" "$run_root/sse"
rm -rf -- "$export_dir"
install -d -o "$export_uid" -g "$export_gid" -m 0700 "$export_dir"

run_log="$run_root/matrix.log"
qa_log="$run_root/qa-command.log"
sse_report="$run_root/sse/sse.json"
sse_summary="$run_root/sse/matrix-summary.json"
ready_file="$run_root/sse/ready.json"
poll_log="$run_root/sse/recovery-polling.log"
health_log="$run_root/sse/recovery-health.log"
fault_log="$run_root/sse/fault.log"
recovery_summary="$run_root/sse/recovery-summary.json"
server_log="$run_root/server-observability.log"
qa_pid=""
poll_pid=""

stop_children() {
  set +e
  if [[ "$qa_pid" =~ ^[1-9][0-9]*$ ]]; then
    kill -TERM "$qa_pid" 2>/dev/null || true
  fi
  if [[ "$poll_pid" =~ ^[1-9][0-9]*$ ]]; then
    kill -TERM "$poll_pid" 2>/dev/null || true
  fi
}
trap stop_children EXIT INT TERM

probe_health() {
  local phase="$1"
  local epoch status
  epoch="$(date +%s)"
  status="$(curl --silent --show-error --max-time 3 -o /dev/null -w '%{http_code}' \
    http://127.0.0.1:8010/api/v1/health/ready 2>/dev/null || true)"
  printf '%s %s %s\n' "$epoch" "$phase" "${status:-000}" >> "$health_log"
  [[ "${status:-000}" == 200 ]]
}

{
  echo "failure_recovery_started=$(date --iso-8601=seconds)"
  echo "fault=$fault run_id=$run_id target_sha=$target_sha"
  echo "sse_connections=$SSE_CONNECTIONS sse_duration=$SSE_DURATION"
} | tee "$run_log"

set +e
timeout --signal=TERM --kill-after=30s "$MAX_RUNTIME" \
  "$QA_PYTHON" "$TOOLS_DIR/platform_sse_qa.py" \
    --env-file "$RUNTIME_ROOT/shared/.env.platform" \
    --origin "$EXPECTED_ORIGIN" --request-origin "$EXPECTED_ORIGIN" \
    --mode sse --control-email "$control_email" --target-sha "$target_sha" \
    --github-run-id "$run_id" --users-per-tournament 32 \
    --sse-connections "$SSE_CONNECTIONS" --sse-duration "$SSE_DURATION" \
    --sse-open-concurrency 32 --sse-open-timeout "$SSE_OPEN_TIMEOUT" \
    --sse-open-rate "$SSE_OPEN_RATE" --sse-capacity-limit 0 \
    --sse-reconnect-cycles 1 --sse-event-count 2 --sse-event-interval 1 \
    --sse-admission-mode ticket --concurrency 8 --http-max-connections 128 \
    --http-timeout 10 --ready-file "$ready_file" \
    --report-path "$sse_report" --summary-path "$sse_summary" --keep-data \
    > "$qa_log" 2>&1 &
qa_pid="$!"
set -e

ready_deadline=$(( $(date +%s) + 120 ))
while [[ ! -s "$ready_file" ]] && kill -0 "$qa_pid" 2>/dev/null; do
  (( $(date +%s) < ready_deadline )) || break
  sleep 1
done

fault_start=0
fault_end=0
fault_status=1
recovery_ready_epoch=0
recovery_subscribers=0
tournament_id=""
tournament_slug=""

if [[ -s "$ready_file" ]]; then
  readarray -t fixture_values < <("$SYSTEM_PYTHON" -I - "$ready_file" <<'PY'
import json
from pathlib import Path
import re
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
tournament_id = str(payload.get("tournament_id") or "")
tournament_slug = str(payload.get("tournament_slug") or "")
if not re.fullmatch(r"[0-9a-fA-F-]{36}", tournament_id):
    raise SystemExit("invalid tournament id")
if not re.fullmatch(r"[A-Za-z0-9_-]+", tournament_slug):
    raise SystemExit("unsafe tournament slug")
print(tournament_id)
print(tournament_slug)
PY
  )
  tournament_id="${fixture_values[0]}"
  tournament_slug="${fixture_values[1]}"
  poll_url="$EXPECTED_ORIGIN/api/v1/tournaments/$tournament_slug/bracket?teams_view=summary"
  (
    set +e
    while kill -0 "$qa_pid" 2>/dev/null; do
      epoch="$(date +%s)"
      status="$(curl --silent --show-error --max-time 5 -o /dev/null -w '%{http_code}' \
        "$poll_url" 2>/dev/null || true)"
      printf '%s %s\n' "$epoch" "${status:-000}"
      sleep 1
    done
  ) > "$poll_log" 2>&1 &
  poll_pid="$!"
  probe_health baseline || true
  if [[ "$fault" == mass-disconnect ]]; then
    # The QA client has one bounded, synchronized reconnect cycle. This fault
    # is deliberately client-side: it exercises mass close/reconnect without
    # killing the server or fabricating an origin outage.
    sleep 45
  else
    sleep 30
  fi
  fault_start="$(date +%s)"
  set +e
  {
    echo "fault_started=$fault_start fault=$fault"
    case "$fault" in
      api-worker-restart)
        systemctl is-active --quiet deadlock-worker.service
        systemctl restart deadlock-worker.service
        systemctl is-active --quiet deadlock-worker.service
        ;;
      api-restart)
        systemctl is-active --quiet deadlock-api.service
        systemctl restart deadlock-api.service
        systemctl is-active --quiet deadlock-api.service
        ;;
      redis-hiccup)
        systemctl is-active --quiet redis-server.service
        systemctl restart redis-server.service
        systemctl is-active --quiet redis-server.service
        ;;
      nginx-reload)
        nginx -t
        systemctl reload nginx.service
        systemctl is-active --quiet nginx.service
        ;;
      mass-disconnect)
        echo "QA client reconnect cycle is the bounded mass-disconnect fault."
        ;;
    esac
  } 2>&1 | tee -a "$fault_log" "$run_log"
  fault_status="${PIPESTATUS[0]}"
  set -e
  fault_end="$(date +%s)"
  echo "fault_finished=$fault_end status=$fault_status" | tee -a "$fault_log" "$run_log"

  recovery_deadline=$(( fault_end + RECOVERY_TIMEOUT ))
  while (( $(date +%s) <= recovery_deadline )); do
    if probe_health post_fault; then
      recovery_ready_epoch="$(date +%s)"
      break
    fi
    sleep 1
  done
  if (( recovery_ready_epoch > 0 )); then
    sleep 10
    for revision in 1 2 3; do
      publish_output="$("$QA_PYTHON" "$TOOLS_DIR/platform_sse_publish_probe.py" \
        --env-file "$RUNTIME_ROOT/shared/.env.platform" \
        --tournament-id "$tournament_id" --revision "$revision" 2>&1)" || true
      echo "$publish_output" | tee -a "$run_log"
      subscribers="$($SYSTEM_PYTHON -I - "$publish_output" <<'PY'
import json
import sys

try:
    print(int(json.loads(sys.argv[1]).get("subscribers") or 0))
except (IndexError, TypeError, ValueError, json.JSONDecodeError):
    print(0)
PY
      )"
      if [[ "$subscribers" =~ ^[0-9]+$ ]] && (( subscribers > recovery_subscribers )); then
        recovery_subscribers="$subscribers"
      fi
      (( recovery_subscribers > 0 )) && break
      sleep 5
    done
  fi
else
  echo "SSE fixture did not reach readiness; no fault was injected." | tee -a "$run_log"
fi

set +e
wait "$qa_pid"
qa_status="$?"
if [[ "$poll_pid" =~ ^[1-9][0-9]*$ ]]; then
  wait "$poll_pid" 2>/dev/null || true
fi
set -e
echo "qa_exit_code=$qa_status" | tee -a "$run_log"

poll_successes_post_fault="$($SYSTEM_PYTHON -I - "$poll_log" "$fault_end" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
cutoff = int(sys.argv[2])
count = 0
if path.is_file():
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                if int(parts[0]) >= cutoff and 200 <= int(parts[1]) < 400:
                    count += 1
            except ValueError:
                pass
print(count)
PY
)"

{
  echo "failure_recovery_finished=$(date --iso-8601=seconds)"
  echo "fault=$fault target_sha=$target_sha"
  echo "deadlock-api=$(systemctl is-active deadlock-api.service 2>&1 || true)"
  echo "deadlock-worker=$(systemctl is-active deadlock-worker.service 2>&1 || true)"
  echo "nginx=$(systemctl is-active nginx.service 2>&1 || true)"
  echo "redis-server=$(systemctl is-active redis-server.service 2>&1 || true)"
  echo "--- sockets ---"
  ss -s 2>&1 || true
  echo "--- recent API journal ---"
  journalctl -u deadlock-api --since "-5 min" --no-pager -o short-iso -n 500 2>&1 || true
  echo "--- recent worker journal ---"
  journalctl -u deadlock-worker --since "-5 min" --no-pager -o short-iso -n 300 2>&1 || true
  echo "--- recent Nginx errors ---"
  tail -n 300 /var/log/nginx/platform-error.log 2>&1 || true
} > "$server_log"

set +e
"$SYSTEM_PYTHON" -I - "$sse_summary" "$recovery_summary" "$target_sha" "$run_id" "$fault" \
  "$qa_status" "$fault_status" "$fault_start" "$fault_end" \
  "$recovery_ready_epoch" "$recovery_subscribers" "$poll_successes_post_fault" \
  "$SSE_CONNECTIONS" <<'PY'
import json
from pathlib import Path
import sys

(
    summary_path,
    recovery_path,
    target_sha,
    run_id,
    fault,
    qa_status,
    fault_status,
    fault_start,
    fault_end,
    recovery_ready_epoch,
    recovery_subscribers,
    poll_successes,
    target_connections,
) = sys.argv[1:]

try:
    summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    summary = {}
if not isinstance(summary, dict):
    summary = {}
sse = summary.get("sse") if isinstance(summary.get("sse"), dict) else {}
reconnects = int(sse.get("reconnects") or 0)
initial_connected = int(sse.get("connected") or 0)
expected_client_reconnect = fault == "mass-disconnect"
recovery = {
    "passed": (
        int(fault_status) == 0
        and int(recovery_ready_epoch) > 0
        and int(poll_successes) > 0
        and int(recovery_subscribers) > 0
        and initial_connected > 0
        and (not expected_client_reconnect or reconnects > 0)
    ),
    "fault": fault,
    "fault_injection_status": int(fault_status),
    "qa_exit_code": int(qa_status),
    "qa_passed": summary.get("passed") is True,
    "expected_fault_disconnects": fault in {"api-restart", "redis-hiccup", "mass-disconnect"},
    "target_connections": int(target_connections),
    "initial_connected": initial_connected,
    "sse_reconnects": reconnects,
    "sse_errors": int(sse.get("errors") or 0),
    "polling_2xx_after_fault": int(poll_successes),
    "recovery_ready_epoch": int(recovery_ready_epoch),
    "recovery_subscribers": int(recovery_subscribers),
    "fault_started_epoch": int(fault_start),
    "fault_finished_epoch": int(fault_end),
    "target_sha": target_sha,
    "github_run_id": int(run_id),
}
summary["recovery"] = recovery
Path(recovery_path).write_text(json.dumps(recovery, indent=2) + "\n", encoding="utf-8")
Path(summary_path).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(recovery, sort_keys=True))
raise SystemExit(0 if recovery["passed"] else 1)
PY
recovery_status="$?"
set -e

find "$run_root" -xdev -type f -links 1 \
  -exec chown root:root -- {} + \
  -exec chmod 0600 -- {} +
install -o "$export_uid" -g "$export_gid" -m 0600 "$sse_summary" "$export_dir/matrix-summary.json"
install -o "$export_uid" -g "$export_gid" -m 0600 "$run_log" "$export_dir/matrix.log"
install -o "$export_uid" -g "$export_gid" -m 0600 "$recovery_summary" "$export_dir/recovery-summary.json"
install -o "$export_uid" -g "$export_gid" -m 0600 "$qa_log" "$export_dir/qa-command.log"
install -o "$export_uid" -g "$export_gid" -m 0600 "$server_log" "$export_dir/server-observability.log"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_EXPORT=%s\n' "$export_dir"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_SUMMARY=%s\n' "$export_dir/matrix-summary.json"
printf 'PRODUCTION_RETAINED_LOAD_MATRIX_RUN_ROOT=%s\n' "$run_root"
printf 'PRODUCTION_SSE_FAILURE_RECOVERY_SUMMARY=%s\n' "$export_dir/recovery-summary.json"
printf 'PRODUCTION_SSE_FAILURE_RECOVERY_EXIT_CODE=%s\n' "$recovery_status"
if [[ "$recovery_status" == 0 ]]; then
  printf 'PRODUCTION_SSE_FAILURE_RECOVERY_PASSED=1\n'
fi
exit "$recovery_status"
