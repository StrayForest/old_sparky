#!/usr/bin/env bash
set +x
set -euo pipefail

TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_production_distributed_ready_check_qa.sh"
COORDINATOR="$TOOLS_DIR/platform_distributed_ready_check_sse.py"
QA_PYTHON="$PLATFORM_ROOT/.venv_platform/bin/python"
RUNTIME_ROOT="/opt/oldsparky/platform"
OUTPUT_ROOT_BASE="$RUNTIME_ROOT/shared/production-retained-matrix"
SYSTEM_PYTHON="/usr/bin/python3.12"
CONFIRMATION="RUN-PRODUCTION-DISTRIBUTED-READY-CHECK-SSE"
LOCK_PATH="/run/lock/oldsparky-retained-load-matrix.lock"
EXPECTED_ORIGIN="https://old-sparky.com"
MAX_RUNTIME="180m"
FIXTURE_WAIT_SECONDS=1500

if [[ "$EUID" -ne 0 ]]; then
  echo "Distributed Ready Check supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Distributed Ready Check must run from the fixed root-controlled checkout." >&2
  exit 1
fi
exec 9>"$LOCK_PATH"
flock -n 9 || {
  echo "Another retained load or cleanup operation is already running on this host." >&2
  exit 1
}

if (( $# != 11 )) || [[ "$1" != "$CONFIRMATION" ]]; then
  echo "Usage: $0 $CONFIRMATION <target-sha> <control-email> <run-id> <shards> <connections-per-shard> <duration> <open-timeout> <capacity-limit> <barrier-timeout> start" >&2
  exit 2
fi

confirmation="$1"
target_sha="$2"
control_email="$3"
run_id="$4"
shards="$5"
connections_per_shard="$6"
duration="$7"
open_timeout="$8"
capacity_limit="$9"
barrier_timeout="${10}"
operation="${11}"

[[ "$confirmation" == "$CONFIRMATION" ]] || exit 1
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "Target SHA must be a lowercase 40-character commit SHA." >&2; exit 1; }
[[ "$control_email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || { echo "Control email is invalid." >&2; exit 1; }
[[ "$run_id" =~ ^[0-9]+$ ]] || { echo "Run id must be numeric." >&2; exit 1; }
[[ "$shards" =~ ^[1-9][0-9]?$ ]] && (( shards <= 32 )) || { echo "Shards must be between 1 and 32." >&2; exit 1; }
[[ "$connections_per_shard" =~ ^[1-9][0-9]{0,3}$ ]] && (( connections_per_shard <= 2000 )) || { echo "Connections per shard must be between 1 and 2000." >&2; exit 1; }
(( shards * connections_per_shard <= 30000 )) || { echo "Distributed target exceeds the QA SSE maximum." >&2; exit 1; }
[[ "$duration" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "Duration must be numeric." >&2; exit 1; }
[[ "$open_timeout" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "Open timeout must be numeric." >&2; exit 1; }
[[ "$capacity_limit" =~ ^[0-9]+$ ]] && (( capacity_limit >= shards * connections_per_shard && capacity_limit <= 30000 )) || { echo "Capacity limit must cover the target and be at most 30000." >&2; exit 1; }
[[ "$barrier_timeout" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "Barrier timeout must be numeric." >&2; exit 1; }
[[ "$operation" == "start" ]] || { echo "Only the start operation is supported." >&2; exit 1; }
(( $(awk "BEGIN {print ($duration >= 1 && $duration <= 900)}") == 1 )) || { echo "Duration must be between 1 and 900 seconds." >&2; exit 1; }
(( $(awk "BEGIN {print ($open_timeout >= 0.5 && $open_timeout <= 60)}") == 1 )) || { echo "Open timeout must be between 0.5 and 60 seconds." >&2; exit 1; }
(( $(awk "BEGIN {print ($barrier_timeout >= 60 && $barrier_timeout <= 3600)}") == 1 )) || { echo "Barrier timeout must be between 60 and 3600 seconds." >&2; exit 1; }

test -d "$TRUSTED_REPO_ROOT/.git" || { echo "Trusted production checkout is missing." >&2; exit 1; }
test -x "$QA_PYTHON" || { echo "Production QA Python runtime is missing." >&2; exit 1; }
test -f "$COORDINATOR" || { echo "Distributed Ready Check coordinator is missing." >&2; exit 1; }
test -L "$RUNTIME_ROOT/current" || { echo "Active production release is missing." >&2; exit 1; }

checkout_sha="$(git -C "$TRUSTED_REPO_ROOT" rev-parse --verify HEAD)"
test "$checkout_sha" = "$target_sha" || { echo "Trusted production checkout does not match target SHA." >&2; exit 1; }
release_sha="$($SYSTEM_PYTHON -I - "$RUNTIME_ROOT/current/RELEASE.json" <<'PY'
import json
from pathlib import Path
import sys
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("source_git_commit", ""))
PY
)"
test "$release_sha" = "$target_sha" || { echo "Active production release does not match target SHA." >&2; exit 1; }
platform_environment="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_ENVIRONMENT)"
test "$platform_environment" = "production" || { echo "Distributed Ready Check requires production environment." >&2; exit 1; }
platform_origin="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_WEB_ORIGIN)"
test "$platform_origin" = "$EXPECTED_ORIGIN" || { echo "Distributed Ready Check requires canonical production origin." >&2; exit 1; }

run_root="$OUTPUT_ROOT_BASE/gha-$run_id"
distributed_root="$run_root/distributed"
if [[ -e "$run_root" || -L "$run_root" ]]; then
  echo "A distributed Ready Check run already exists for this GitHub run id." >&2
  exit 1
fi
install -d -o root -g root -m 0700 "$distributed_root"
log_path="$run_root/qa-command.log"

nohup /usr/bin/setsid /usr/bin/timeout --signal=TERM --kill-after=30s "$MAX_RUNTIME" \
  "$QA_PYTHON" "$COORDINATOR" \
    --role coordinator \
    --env-file "$RUNTIME_ROOT/shared/.env.platform" \
    --run-id "$run_id" \
    --run-root "$run_root" \
    --target-sha "$target_sha" \
    --origin "$EXPECTED_ORIGIN" \
    --request-origin "$EXPECTED_ORIGIN" \
    --fixture-origin "http://127.0.0.1:8010" \
    --control-email "$control_email" \
    --shards "$shards" \
    --connections-per-shard "$connections_per_shard" \
    --capacity-limit "$capacity_limit" \
    --duration "$duration" \
    --open-timeout "$open_timeout" \
    --barrier-timeout "$barrier_timeout" \
  </dev/null >"$log_path" 2>&1 &
coordinator_pid="$!"
printf '%s\n' "$coordinator_pid" > "$run_root/coordinator.pid"
chmod 0600 "$run_root/coordinator.pid" "$log_path" 2>/dev/null || true

deadline=$(( $(date +%s) + FIXTURE_WAIT_SECONDS ))
control_path="$distributed_root/control.json"
while (( $(date +%s) < deadline )); do
  if [[ -s "$control_path" ]]; then
    printf 'PRODUCTION_DISTRIBUTED_READY_CHECK_RUN_ROOT=%s\n' "$run_root"
    printf 'PRODUCTION_DISTRIBUTED_READY_CHECK_CONTROL=%s\n' "$control_path"
    printf 'PRODUCTION_DISTRIBUTED_READY_CHECK_PID=%s\n' "$coordinator_pid"
    exit 0
  fi
  if ! kill -0 "$coordinator_pid" 2>/dev/null; then
    echo "Distributed Ready Check coordinator exited before fixture barrier." >&2
    tail -n 120 "$log_path" >&2 || true
    exit 1
  fi
  sleep 2
done
echo "Distributed Ready Check fixture did not become ready within the bounded setup window." >&2
tail -n 120 "$log_path" >&2 || true
exit 1
