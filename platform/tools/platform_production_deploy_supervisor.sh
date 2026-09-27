#!/usr/bin/env bash
set +x

# Root-side production deployment supervisor. The workflow reaches this
# helper through the fixed remote dispatcher, never through SSH shell text.
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
# Explicit ``-B`` flags below are the primary contract.  Keep this defense
# for nested non-isolated helpers as well, so no Python child writes into the
# root-owned immutable generation.
export PYTHONDONTWRITEBYTECODE=1

invalid_input() {
  printf '%s\n' 'ERROR: deployment input is invalid' >&2
  exit 2
}

if (( $# != 5 )); then
  invalid_input
fi
target_sha="$1"
release_slug="$2"
deploy_mode="$3"
artifact_dir="$4"
runtime_profile="$5"

runtime=/opt/oldsparky/platform
current="$runtime/current"
host_tools_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
storage_summary_tool=""
artifact_path=""
provenance_path="$artifact_dir/RELEASE.provenance.json"
bootstrap_dir=""

failure_class="preflight"
failure_phase="preflight"
failure_reason="internal"

set_failure_context() {
  failure_class="$1"
  failure_phase="$2"
  failure_reason="$3"
}

fail() {
  # Failure detail remains private machine state. The runner only
  # accepts the fixed, token-only RELEASE_DEPLOY line below.
  printf '%s\n' 'ERROR: deployment failed' >&2
  printf 'RELEASE_DEPLOY schema=1 status=failed class=%s phase=%s reason=%s release_slug=%s source_sha=%s\n' \
    "$failure_class" "$failure_phase" "$failure_reason" "$release_slug" "$target_sha"
  exit 1
}

cleanup() {
  local cleanup_rc=$?
  trap - EXIT
  set +e
  if [[ "$artifact_dir" =~ ^/tmp/old-sparky-platform-artifact-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}$ \
    && -d "$artifact_dir" && ! -L "$artifact_dir" ]]; then
    rm -rf -- "$artifact_dir" || cleanup_rc=1
  fi
  if [[ -n "$bootstrap_dir" && -d "$bootstrap_dir" && ! -L "$bootstrap_dir" ]]; then
    rm -rf -- "$bootstrap_dir" || cleanup_rc=1
  fi
  if declare -F platform_retained_load_lock_close >/dev/null 2>&1; then
    platform_retained_load_lock_close
  fi
  if declare -F platform_release_lock_close >/dev/null 2>&1; then
    platform_release_lock_close
  fi
  exit "$cleanup_rc"
}
trap cleanup EXIT

[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || invalid_input
[[ "$release_slug" =~ ^gha-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}-[0-9a-f]{12}$ ]] || invalid_input
[[ "$release_slug" == *"-${target_sha:0:12}" ]] || invalid_input
[[ "$artifact_dir" =~ ^/tmp/old-sparky-platform-artifact-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}$ ]] || invalid_input
case "$deploy_mode" in
  preflight|deploy) ;;
  *) invalid_input ;;
esac
case "$runtime_profile" in
  baseline|ready-vote-static-4|ready-vote-static-6|ready-vote-static-8|ready-vote-cprofile|ready-vote-static-12|ready-vote-static-16|ready-vote-adaptive-v2|api-3x16|api-1x48|read-mix-cprofile|authenticated-read-admission-32|authenticated-read-admission-24x8|pool-pre-ping-off|web-ssr-diagnostics|web-ssr-native-transport|web-ssr-workers-2|uvicorn-classic|uvicorn-optimized|api-pool-12|api-pool-16|api-pool-20|api-pool-24) ;;
  *) invalid_input ;;
esac

restart_web_and_wait() {
  systemctl restart deadlock-web
  for _ in $(seq 1 30); do
    if systemctl is-active --quiet deadlock-web \
      && curl --fail --silent --show-error --max-time 2 \
        http://127.0.0.1:3000/ >/dev/null; then
      return 0
    fi
    sleep 1
  done
  fail "deadlock-web did not recover after runtime profile"
}

restart_api_and_wait() {
  systemctl restart deadlock-api || return 1
  for _ in $(seq 1 30); do
    if systemctl is-active --quiet deadlock-api \
      && curl --fail --silent --show-error --max-time 2 \
        http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

[[ "$host_tools_dir" =~ ^/opt/oldsparky/platform/shared/host-tools/[0-9a-f]{40}$ ]] \
  || fail "trusted host-tools generation path is invalid"
[[ -d "$host_tools_dir" && ! -L "$host_tools_dir" ]] \
  || fail "trusted host-tools generation directory is unsafe"
[[ "$(/usr/bin/stat -c '%F:%u:%g:%h:%a' -- "$host_tools_dir" 2>/dev/null)" \
  == "directory:0:0:2:555" ]] \
  || fail "trusted host-tools generation metadata is unsafe"

require_host_helper() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" && -x "$path" ]] || fail "trusted host helper is missing"
  [[ "$(/usr/bin/stat -c '%F:%u:%g:%h:%a' -- "$path" 2>/dev/null)" \
    == "regular file:0:0:1:555" ]] \
    || fail "trusted host helper metadata is unsafe"
}

set_failure_context preflight preflight host_tools_invalid
for host_helper in \
  platform_workflow_remote_dispatch.py \
  platform_workflow_input_guard.py \
  platform_prepare_artifact_dir.py \
  platform_production_deploy_supervisor.sh \
  platform_release_lock.sh \
  platform_release_preflight.sh \
  platform_validate_release_artifact.py \
  platform_safe_env_exec.py \
  platform_render_service_envs.py \
  platform_validate_edge_policy.py \
  platform_update_cloudflare_ips.py \
  platform_storage_evidence_summary.py \
  platform_configure_shared_env.py; do
  require_host_helper "$host_tools_dir/$host_helper"
done

set_failure_context preflight preflight lock
# Lock order is release -> retained-load.  Both locks use the shared pathname
# supervisors with util-linux `--close`, so this body and every candidate child
# have no release or retained-load lock FD.  Each supervised body revalidates
# the exact exclusive WRITE FLOCK owner in /proc/locks before mutation.
lock_helper="$host_tools_dir/platform_release_lock.sh"
if [[ ! -f "$lock_helper" || -L "$lock_helper" || ! -x "$lock_helper" ]]; then
  fail "the canonical release lock helper is missing or unsafe"
fi
ORIGINAL_ARGS=("$@")
# shellcheck source=/dev/null
source "$lock_helper"
platform_release_lock_supervise "${ORIGINAL_ARGS[@]}" || {
  lock_status=$?
  if [[ "$lock_status" -eq "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE" ]]; then
    fail "another platform release or recovery operation is active"
  fi
  exit "$lock_status"
}
if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" != "1" ]]; then
  exit 0
fi
platform_release_lock_open || fail "the canonical release lock could not be validated"
platform_retained_load_lock_supervise "${ORIGINAL_ARGS[@]}" || \
  fail "the retained-load lock supervisor could not be started"
if [[ "${PLATFORM_RETAINED_LOAD_LOCK_SUPERVISED:-}" != "1" ]]; then
  exit 0
fi
platform_retained_load_lock_open \
  || fail "the retained-load lock could not be opened or is already held"

set_failure_context preflight preflight environment
test "$(id -u)" -eq 0 || fail "deployment user must be root"
test -L "$current" || fail "current release symlink is missing"
test -L "$runtime/previous" || fail "previous release symlink is missing"
set_failure_context preflight preflight service_state
for service in deadlock-api deadlock-worker deadlock-web; do
  systemctl is-active --quiet "$service" || fail "$service is not active before deployment"
done

set_failure_context preflight preflight nginx_config
nginx -t >/dev/null

set_failure_context preflight preflight preflight_failed
"$host_tools_dir/platform_release_preflight.sh" \
  --require-previous \
  --require-verified-backup \
  --require-edge-parity \
  --backup-max-age-hours 24 >/dev/null 2>/dev/null

if [[ "$deploy_mode" == "preflight" ]]; then
  printf 'RELEASE_DEPLOY schema=1 status=passed class=preflight release_slug=%s source_sha=%s\n' \
    "$release_slug" "$target_sha"
  exit 0
fi

[[ "$deploy_mode" == "deploy" ]] || fail "unsupported deployment mode"
set_failure_context artifact artifact artifact_missing
if [[ ! -d "$artifact_dir" || -L "$artifact_dir" ]]; then
  fail "CI release artifact directory is missing"
fi

artifact_count="$(find "$artifact_dir" -maxdepth 1 -type f -name '*.tar.gz' -printf 'x\n' | wc -l)"
if [[ "$artifact_count" != "1" ]]; then
  set_failure_context artifact artifact artifact_count_invalid
  fail "CI release artifact count is invalid"
fi
artifact_path="$(find "$artifact_dir" -maxdepth 1 -type f -name '*.tar.gz' -print -quit)"
artifact_checksum="$artifact_path.sha256"
if [[ ! -f "$artifact_checksum" || -L "$artifact_checksum" ]]; then
  set_failure_context artifact artifact checksum_missing
  fail "CI release checksum is missing"
fi
if [[ ! -f "$provenance_path" || -L "$provenance_path" ]]; then
  set_failure_context artifact provenance provenance_missing
  fail "CI release provenance is missing"
fi
artifact_name="$(basename "$artifact_path")"
artifact_slug="$(basename "$artifact_path" .tar.gz)"
if [[ ! "$artifact_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}\.tar\.gz$ ]]; then
  set_failure_context artifact artifact artifact_name_invalid
  fail "CI release artifact name is invalid"
fi
if [[ "$artifact_slug" != "$release_slug" ]]; then
  set_failure_context artifact provenance release_slug_mismatch
  fail "CI release artifact slug does not match the deployment slug"
fi
(cd "$artifact_dir" && sha256sum -c "$(basename "$artifact_checksum")") \
  || {
    set_failure_context artifact artifact checksum_mismatch
    fail "CI release artifact digest mismatch"
  }
bootstrap_dir="$(mktemp -d /tmp/old-sparky-release-bootstrap.XXXXXX)"
chmod 0700 "$bootstrap_dir"
/usr/bin/python3 -I -B "$host_tools_dir/platform_validate_release_artifact.py" \
  --artifact "$artifact_path" \
  --checksum "$artifact_checksum" \
  --release-slug "$artifact_slug" \
  --extract-to "$bootstrap_dir" \
  || {
    set_failure_context artifact artifact validation_failed
    fail "CI release artifact provenance is invalid"
  }
if ! /usr/bin/python3 -I -B - "$artifact_path" "$artifact_slug" "$target_sha" "$provenance_path" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile

artifact, slug, expected_commit, provenance_path = sys.argv[1:]
def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result

def reject():
    raise ValueError

try:
    provenance = json.loads(
        Path(provenance_path).read_text(encoding="utf-8"),
        object_pairs_hook=strict_object,
    )
    if (
        not isinstance(provenance, dict)
        or set(provenance)
        != {"schema", "artifact_file", "artifact_sha256", "source_git_commit"}
        or type(provenance["schema"]) is not int
        or provenance["schema"] != 1
    ):
        reject()
    artifact_name = Path(artifact).name
    if (
        not isinstance(provenance["artifact_file"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}\.tar\.gz", artifact_name)
        is None
        or provenance["artifact_file"] != artifact_name
        or artifact_name != f"{slug}.tar.gz"
    ):
        reject()
    if (
        not isinstance(provenance["artifact_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", provenance["artifact_sha256"]) is None
    ):
        reject()
    if (
        not isinstance(expected_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None
        or not isinstance(provenance["source_git_commit"], str)
        or provenance["source_git_commit"] != expected_commit
    ):
        reject()
    if (
        not isinstance(slug, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}", slug) is None
    ):
        reject()
    actual_sha256 = hashlib.sha256(Path(artifact).read_bytes()).hexdigest()
    if provenance["artifact_sha256"] != actual_sha256:
        reject()
    with tarfile.open(Path(artifact), mode="r:gz") as archive:
        member = archive.getmember(f"{slug}/RELEASE.json")
        handle = archive.extractfile(member)
        if handle is None:
            reject()
        payload = json.load(handle, object_pairs_hook=strict_object)
    if (
        not isinstance(payload, dict)
        or payload.get("release_slug") != slug
        or payload.get("source_git_commit") != expected_commit
    ):
        reject()
except (OSError, KeyError, TypeError, ValueError, UnicodeError, tarfile.TarError):
    raise SystemExit("release provenance is invalid")
PY
then
  set_failure_context artifact provenance provenance_invalid
  fail "CI release source commit does not match target SHA"
fi

set_failure_context preflight preflight preflight_failed
"$host_tools_dir/platform_release_preflight.sh" \
  --app-dir "$runtime" \
  --require-previous \
  --require-verified-backup \
  --require-edge-parity \
  --backup-max-age-hours 24 >/dev/null 2>/dev/null

set_failure_context deployment candidate candidate_missing
candidate_deploy="$bootstrap_dir/$artifact_slug/tools/platform_release_deploy.sh"
if [[ ! -f "$candidate_deploy" || -L "$candidate_deploy" || ! -x "$candidate_deploy" ]]; then
  fail "candidate release deploy tool is missing"
fi
storage_summary_tool="$host_tools_dir/platform_storage_evidence_summary.py"
candidate_status=0
LC_ALL=C.UTF-8 "$candidate_deploy" \
  --artifact "$artifact_path" \
  --app-dir "$runtime" \
  --edge-origin https://127.0.0.1 \
  --edge-host old-sparky.com \
  --expected-csp-mode enforce >/dev/null 2>/dev/null || candidate_status=$?
if (( candidate_status != 0 )); then
  set_failure_context deployment candidate activation_failed
  summarize_candidate_failure() {
    printf '{"schema":1,"kind":"candidate_activation_failure","status":"failed","error_class":"activation","exit_status":%s,"target_sha":"%s"}\n' \
      "$candidate_status" "$target_sha"
    printf 'state=held\n' \
      | /usr/bin/python3 -I -B "$storage_summary_tool" --mode lock
    emit_candidate_disk() {
      local category="$1"
      local path="$2"
      local disk_output inode_output
      disk_output="$(df -B1 --output=size,used,avail,pcent -- "$path" 2>/dev/null)" \
        || return 1
      printf '%s\n' "$disk_output" \
        | /usr/bin/python3 -I -B "$storage_summary_tool" --mode df --category "$category" \
        || return 1
      inode_output="$(df --output=iused,iavail,ipcent -- "$path" 2>/dev/null)" \
        || return 1
      printf '%s\n' "$inode_output" \
        | /usr/bin/python3 -I -B "$storage_summary_tool" --mode inode --category "$category" \
        || return 1
    }
    emit_candidate_disk root /
    emit_candidate_disk tmp /tmp
    emit_candidate_disk var_tmp /var/tmp
    emit_candidate_disk logs /var/log
    for service in deadlock-api deadlock-worker deadlock-web; do
      local service_output
      service_output="$(systemctl show "$service" \
        --property=ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,NRestarts,MemoryCurrent,MemoryPeak,MemoryMax,TasksCurrent,TasksMax,CPUUsageNSec \
        --no-pager 2>/dev/null)" \
        || return 1
      printf '%s\n' "$service_output" \
        | /usr/bin/python3 -I -B "$storage_summary_tool" --mode service --service "$service" \
        || return 1
    done
  }
  if ! summarize_candidate_failure; then
    fail "candidate release activation failed; diagnostic summary unavailable"
  fi
  fail "candidate release activation failed; diagnostics summarized"
fi

# The candidate deploy process has returned, but the release lock
# remains held by this shell for all runtime-profile writes,
# restarts/readiness checks and final smoke evidence below.
platform_release_lock_supervisor_holds \
  || {
    set_failure_context deployment candidate lock_lost
    fail "the platform release lock was lost"
  }

set_failure_context deployment readiness runtime_profile_failed
case "$runtime_profile" in
  baseline)
    "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
      --apply \
      --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
      --profile baseline \
      --only PLATFORM_LOG_LEVEL \
      --only PLATFORM_PERF_LOG_ENABLED \
      --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED \
      --only PLATFORM_API_WORKERS \
      --only PLATFORM_DB_POOL_SIZE \
      --only PLATFORM_DB_MAX_OVERFLOW \
      --only PLATFORM_DB_CONNECTION_BUDGET \
      --only PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY \
      --only PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY \
      --only PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY \
      --only PLATFORM_WEB_WORKERS \
      --only PLATFORM_WEB_SERVER_AUTH_TRANSPORT \
      --only PLATFORM_SSR_PERF_LOG_ENABLED \
      --only PLATFORM_SSR_PERF_SAMPLE_RATE \
      --only PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS
    systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if systemctl is-active --quiet deadlock-api \
        && curl --fail --silent --show-error --max-time 2 \
          http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
        api_ready=true
        break
      fi
      sleep 1
    done
    [[ "$api_ready" == true ]] \
      || fail "API did not recover on baseline runtime profile"
    ;;
  ready-vote-static-4|ready-vote-static-6|ready-vote-static-8|ready-vote-static-12|ready-vote-static-16|ready-vote-cprofile)
    ready_vote_profile_args=(
      --only PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY
      --only PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY
      --only PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY
      --only PLATFORM_WEB_WORKERS
      --only PLATFORM_WEB_SERVER_AUTH_TRANSPORT
      --only PLATFORM_LOG_LEVEL
      --only PLATFORM_PERF_LOG_ENABLED
      --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED
      --only PLATFORM_SSR_PERF_LOG_ENABLED
      --only PLATFORM_SSR_PERF_SAMPLE_RATE
      --only PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS
    )
    if [[ "$runtime_profile" == ready-vote-cprofile ]]; then
      ready_vote_profile_args+=(
        --only PLATFORM_READY_VOTE_CPU_PROFILE_DIR
        --only PLATFORM_PERF_SLOW_REQUEST_MS
        --only PLATFORM_PERF_LOG_MUTATIONS
      )
    else
      # A static profile must also clear one-shot diagnostic
      # overrides left by a preceding cProfile deployment.
      ready_vote_profile_args+=(
        --only PLATFORM_READY_VOTE_CPU_PROFILE_DIR
        --only PLATFORM_PERF_SLOW_REQUEST_MS
        --only PLATFORM_PERF_LOG_MUTATIONS
      )
    fi
    "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
      --apply \
      --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
      --profile "$runtime_profile" \
      "${ready_vote_profile_args[@]}"
    systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if systemctl is-active --quiet deadlock-api \
        && curl --fail --silent --show-error --max-time 2 \
          http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
        api_ready=true
        break
      fi
      sleep 1
    done
    if [[ "$api_ready" != true ]]; then
      restore_ready_vote_profile_args=(
        --only PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY
        --only PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY
        --only PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY
        --only PLATFORM_WEB_WORKERS
        --only PLATFORM_WEB_SERVER_AUTH_TRANSPORT
        --only PLATFORM_LOG_LEVEL
        --only PLATFORM_PERF_LOG_ENABLED
        --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED
        --only PLATFORM_SSR_PERF_LOG_ENABLED
        --only PLATFORM_SSR_PERF_SAMPLE_RATE
        --only PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS
      )
      if [[ "$runtime_profile" == ready-vote-cprofile ]]; then
        restore_ready_vote_profile_args+=(
          --only PLATFORM_READY_VOTE_CPU_PROFILE_DIR
          --only PLATFORM_PERF_SLOW_REQUEST_MS
          --only PLATFORM_PERF_LOG_MUTATIONS
        )
      else
        restore_ready_vote_profile_args+=(
          --only PLATFORM_READY_VOTE_CPU_PROFILE_DIR
          --only PLATFORM_PERF_SLOW_REQUEST_MS
          --only PLATFORM_PERF_LOG_MUTATIONS
        )
      fi
      "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
        --apply \
        --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
        --profile baseline \
        "${restore_ready_vote_profile_args[@]}"
      systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if systemctl is-active --quiet deadlock-api \
          && curl --fail --silent --show-error --max-time 2 \
            http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
          api_ready=true
          break
        fi
        sleep 1
      done
      [[ "$api_ready" == true ]] \
        || fail "static Ready Vote profile health check failed; baseline restore failed"
      fail "static Ready Vote profile health check failed; baseline restored"
    fi
    ;;
  ready-vote-adaptive-v2)
    "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
      --apply \
      --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
      --profile "$runtime_profile" \
      --only PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY \
      --only PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY \
      --only PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY \
      --only PLATFORM_READY_VOTE_ADMISSION_MAX_WAITERS \
      --only PLATFORM_READY_VOTE_ADMISSION_WAIT_TIMEOUT_MS \
      --only PLATFORM_READY_VOTE_ADMISSION_CPU_SAMPLE_INTERVAL_SECONDS \
      --only PLATFORM_READY_VOTE_ADMISSION_CPU_EWMA_ALPHA \
      --only PLATFORM_READY_VOTE_ADMISSION_RECOVERY_SAMPLES \
      --only PLATFORM_READY_VOTE_ADMISSION_CONTROL_INTERVAL_SECONDS \
      --only PLATFORM_LOG_LEVEL \
      --only PLATFORM_PERF_LOG_ENABLED \
      --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED \
      --only PLATFORM_SSR_PERF_LOG_ENABLED \
      --only PLATFORM_SSR_PERF_SAMPLE_RATE \
      --only PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS
    systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if systemctl is-active --quiet deadlock-api \
        && curl --fail --silent --show-error --max-time 2 \
          http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
        api_ready=true
        break
      fi
      sleep 1
    done
    if [[ "$api_ready" != true ]]; then
      "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
        --apply \
        --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
        --profile ready-vote-static-8 \
        --only PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY \
        --only PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY \
        --only PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY \
        --only PLATFORM_LOG_LEVEL \
        --only PLATFORM_PERF_LOG_ENABLED \
        --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED \
        --only PLATFORM_SSR_PERF_LOG_ENABLED \
        --only PLATFORM_SSR_PERF_SAMPLE_RATE \
        --only PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS
      systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if systemctl is-active --quiet deadlock-api \
          && curl --fail --silent --show-error --max-time 2 \
            http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
          api_ready=true
          break
        fi
        sleep 1
      done
      [[ "$api_ready" == true ]] \
        || fail "adaptive-v2 health check failed; static-8 restore failed"
      fail "adaptive-v2 health check failed; static-8 restored"
    fi
    ;;
  web-ssr-diagnostics)
    web_ssr_profile_args=(
      --only PLATFORM_WEB_WORKERS
      --only PLATFORM_WEB_SERVER_AUTH_TRANSPORT
      --only PLATFORM_LOG_LEVEL
      --only PLATFORM_PERF_LOG_ENABLED
      --only PLATFORM_SSR_PERF_LOG_ENABLED
      --only PLATFORM_SSR_PERF_SAMPLE_RATE
      --only PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS
      --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED
    )
    "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
      --apply \
      --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
      --profile "$runtime_profile" \
      "${web_ssr_profile_args[@]}"
    api_env="$runtime/shared/env/api.env"
    grep -qx 'PLATFORM_LOG_LEVEL=INFO' "$api_env" \
      || fail "web SSR diagnostic API env did not select INFO log level"
    grep -qx 'PLATFORM_PERF_LOG_ENABLED=true' "$api_env" \
      || fail "web SSR diagnostic API perf logging is not enabled"
    grep -qx 'PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED=true' "$api_env" \
      || fail "web SSR diagnostic API auth bootstrap log gate is not enabled"
    if ! restart_api_and_wait; then
      "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
        --apply \
        --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
        --profile baseline \
        "${web_ssr_profile_args[@]}"
      restart_api_and_wait \
        || fail "web SSR diagnostic API health check failed; baseline restore failed"
      fail "web SSR diagnostic API health check failed; baseline restored"
    fi
    systemctl restart deadlock-web
    web_ready=false
    for _ in $(seq 1 30); do
      if systemctl is-active --quiet deadlock-web \
        && curl --fail --silent --show-error --max-time 2 \
          http://127.0.0.1:3000/ >/dev/null; then
        web_ready=true
        break
      fi
      sleep 1
    done
    if [[ "$web_ready" != true ]]; then
      "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
        --apply \
        --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
        --profile baseline \
        "${web_ssr_profile_args[@]}"
      restart_api_and_wait \
        || fail "web SSR diagnostic profile health check failed; API baseline restore failed"
      systemctl restart deadlock-web
      web_ready=false
      for _ in $(seq 1 30); do
        if systemctl is-active --quiet deadlock-web \
          && curl --fail --silent --show-error --max-time 2 \
            http://127.0.0.1:3000/ >/dev/null; then
          web_ready=true
          break
        fi
        sleep 1
      done
      [[ "$web_ready" == true ]] \
        || fail "web SSR diagnostic profile health check failed; baseline restore failed"
      fail "web SSR diagnostic profile health check failed; baseline restored"
    fi
    ;;
  web-ssr-native-transport)
    "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
      --apply \
      --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
      --profile "$runtime_profile" \
      --only PLATFORM_WEB_SERVER_AUTH_TRANSPORT
    restart_web_and_wait
    ;;
  web-ssr-workers-2)
    "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
      --apply \
      --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
      --profile "$runtime_profile" \
      --only PLATFORM_WEB_WORKERS \
      --only PLATFORM_WEB_SERVER_AUTH_TRANSPORT
    restart_web_and_wait
    ;;
  read-mix-cprofile|authenticated-read-admission-32|authenticated-read-admission-24x8|pool-pre-ping-off|uvicorn-classic|uvicorn-optimized|api-pool-12|api-pool-16|api-pool-20|api-pool-24)
    candidate_profile_args=(
      --only PLATFORM_API_WORKERS
      --only PLATFORM_UVICORN_LOOP
      --only PLATFORM_UVICORN_HTTP
      --only PLATFORM_DB_POOL_SIZE
      --only PLATFORM_DB_MAX_OVERFLOW
      --only PLATFORM_DB_POOL_PRE_PING
      --only PLATFORM_DB_CONNECTION_BUDGET
      --only PLATFORM_AUTHENTICATED_READ_ADMISSION_ENABLED
      --only PLATFORM_AUTHENTICATED_READ_ADMISSION_CONCURRENCY
      --only PLATFORM_AUTHENTICATED_READ_ADMISSION_MAX_WAITERS
      --only PLATFORM_AUTHENTICATED_READ_ADMISSION_WAIT_TIMEOUT_MS
      --only PLATFORM_READY_VOTE_CPU_PROFILE_DIR
      --only PLATFORM_PERF_SLOW_REQUEST_MS
      --only PLATFORM_PERF_LOG_MUTATIONS
      --only PLATFORM_LOG_LEVEL
      --only PLATFORM_PERF_LOG_ENABLED
      --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED
      --only PLATFORM_SSR_PERF_LOG_ENABLED
      --only PLATFORM_SSR_PERF_SAMPLE_RATE
      --only PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS
    )
    "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
      --apply \
      --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
      --profile "$runtime_profile" \
      "${candidate_profile_args[@]}"
    systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if systemctl is-active --quiet deadlock-api \
        && curl --fail --silent --show-error --max-time 2 \
          http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
        api_ready=true
        break
      fi
      sleep 1
    done
    if [[ "$api_ready" != true ]]; then
      "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
        --apply \
        --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
        --profile baseline \
        "${candidate_profile_args[@]}"
      systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if systemctl is-active --quiet deadlock-api \
          && curl --fail --silent --show-error --max-time 2 \
            http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
          api_ready=true
          break
        fi
        sleep 1
      done
      [[ "$api_ready" == true ]] \
        || fail "runtime profile health check failed; baseline restore failed"
      fail "runtime profile health check failed; baseline restored"
    fi
    ;;
  api-3x16)
    "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
      --apply \
      --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
      --profile api-3x16 \
      --only PLATFORM_API_WORKERS \
      --only PLATFORM_DB_POOL_SIZE \
      --only PLATFORM_DB_MAX_OVERFLOW \
      --only PLATFORM_DB_CONNECTION_BUDGET \
      --only PLATFORM_LOG_LEVEL \
      --only PLATFORM_PERF_LOG_ENABLED \
      --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED
    systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if systemctl is-active --quiet deadlock-api \
        && curl --fail --silent --show-error --max-time 2 \
          http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
        api_ready=true
        break
      fi
      sleep 1
    done
    if [[ "$api_ready" != true ]]; then
      "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
        --apply \
        --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
        --profile baseline \
        --only PLATFORM_API_WORKERS \
        --only PLATFORM_DB_POOL_SIZE \
        --only PLATFORM_DB_MAX_OVERFLOW \
        --only PLATFORM_DB_CONNECTION_BUDGET \
        --only PLATFORM_LOG_LEVEL \
        --only PLATFORM_PERF_LOG_ENABLED \
        --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED
      systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if systemctl is-active --quiet deadlock-api \
          && curl --fail --silent --show-error --max-time 2 \
            http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
          api_ready=true
          break
        fi
        sleep 1
      done
      [[ "$api_ready" == true ]] \
        || fail "API did not recover on baseline runtime profile"
      fail "runtime profile health check failed; baseline restored"
    fi
    ;;
  api-1x48)
    "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
      --apply \
      --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
      --profile api-1x48 \
      --only PLATFORM_API_WORKERS \
      --only PLATFORM_DB_POOL_SIZE \
      --only PLATFORM_DB_MAX_OVERFLOW \
      --only PLATFORM_DB_CONNECTION_BUDGET \
      --only PLATFORM_LOG_LEVEL \
      --only PLATFORM_PERF_LOG_ENABLED \
      --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED
    systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if systemctl is-active --quiet deadlock-api \
        && curl --fail --silent --show-error --max-time 2 \
          http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
        api_ready=true
        break
      fi
      sleep 1
    done
    if [[ "$api_ready" != true ]]; then
      "$runtime/shared/venv/bin/python" -B "$host_tools_dir/platform_configure_shared_env.py" \
        --apply \
        --confirm APPLY_PUBLIC_PRODUCTION_BASELINE \
        --profile baseline \
        --only PLATFORM_API_WORKERS \
        --only PLATFORM_DB_POOL_SIZE \
        --only PLATFORM_DB_MAX_OVERFLOW \
        --only PLATFORM_DB_CONNECTION_BUDGET \
        --only PLATFORM_LOG_LEVEL \
        --only PLATFORM_PERF_LOG_ENABLED \
        --only PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED
      systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if systemctl is-active --quiet deadlock-api \
          && curl --fail --silent --show-error --max-time 2 \
            http://127.0.0.1:8010/api/v1/health/ready >/dev/null; then
          api_ready=true
          break
        fi
        sleep 1
      done
      [[ "$api_ready" == true ]] \
        || fail "API did not recover on baseline runtime profile"
      fail "runtime profile health check failed; baseline restored"
    fi
    ;;
  *)
    fail "unsupported runtime profile"
    ;;
esac

# SSR diagnostics are read by the Next.js process at startup. Several
# API-oriented runtime profiles also write those keys while updating
# their own limits, so a successful API restart alone can leave the
# web process on the previous diagnostic state.
case "$runtime_profile" in
  baseline|ready-vote-static-*|ready-vote-cprofile|ready-vote-adaptive-v2|read-mix-cprofile|authenticated-read-admission-32|authenticated-read-admission-24x8|pool-pre-ping-off|uvicorn-classic|uvicorn-optimized|api-pool-*)
    restart_web_and_wait
    ;;
esac

printf 'RELEASE_DEPLOY schema=1 status=passed class=deployment release_slug=%s source_sha=%s\n' \
  "$release_slug" "$target_sha"
