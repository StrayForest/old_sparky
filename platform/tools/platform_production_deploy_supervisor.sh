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
NGINX_BIN="/usr/sbin/nginx"
NGINX_TIMEOUT_BIN="/usr/bin/timeout"
NGINX_CONFIG_TIMEOUT_SECONDS=30
SYSTEMCTL_BIN="/usr/bin/systemctl"
SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"
SYSTEMCTL_TIMEOUT_SECONDS=30

invalid_input() {
  printf '%s\n' 'ERROR: deployment input is invalid' >&2
  exit 2
}

if (( $# != 5 && $# != 8 && $# != 9 )); then
  invalid_input
fi
target_sha="$1"
release_slug="$2"
deploy_mode="$3"
artifact_dir="$4"
runtime_profile="$5"
host_tools_sha=""
host_manifest_sha=""
host_capabilities_sha=""
baseline_identity_b64=""
if (( $# == 8 )); then
  host_tools_sha="$6"
  host_manifest_sha="$7"
  host_capabilities_sha="$8"
elif (( $# == 9 )); then
  host_tools_sha="$6"
  host_manifest_sha="$7"
  host_capabilities_sha="$8"
  baseline_identity_b64="$9"
fi

runtime=/opt/oldsparky/platform
current="$runtime/current"
host_tools_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
storage_summary_tool=""
artifact_path=""
provenance_path="$artifact_dir/RELEASE.provenance.json"
bootstrap_dir=""
artifact_cleanup_owned=0
artifact_identity_before_lock=""
artifact_identity_owned=""

failure_class="preflight"
failure_phase="preflight"
failure_reason="internal"
failure_lock_stage=""

set_failure_context() {
  failure_class="$1"
  failure_phase="$2"
  failure_reason="$3"
  failure_lock_stage=""
}

set_lock_failure_context() {
  failure_class="preflight"
  failure_phase="preflight"
  failure_reason="lock"
  failure_lock_stage="$1"
}

run_nginx_config_test() {
  "$NGINX_TIMEOUT_BIN" --foreground --signal=TERM --kill-after=5s \
    "${NGINX_CONFIG_TIMEOUT_SECONDS}s" "$NGINX_BIN" -t
}

run_systemctl() {
  "$SYSTEMCTL_TIMEOUT_BIN" --signal=TERM --kill-after=5s \
    "${SYSTEMCTL_TIMEOUT_SECONDS}s" "$SYSTEMCTL_BIN" "$@"
}

service_is_active() {
  local service="$1" output status
  if output="$(run_systemctl is-active "$service" 2>/dev/null)"; then
    status=0
  else
    status=$?
  fi
  case "$status:$output" in
    0:active) return 0 ;;
    3:inactive) return 3 ;;
    *) return 4 ;;
  esac
}

emit_failure_marker() {
  if [[ -n "$failure_lock_stage" ]]; then
    printf 'RELEASE_DEPLOY schema=1 status=failed class=%s phase=%s reason=%s lock_stage=%s release_slug=%s source_sha=%s\n' \
      "$failure_class" "$failure_phase" "$failure_reason" "$failure_lock_stage" "$release_slug" "$target_sha"
  else
    printf 'RELEASE_DEPLOY schema=1 status=failed class=%s phase=%s reason=%s release_slug=%s source_sha=%s\n' \
      "$failure_class" "$failure_phase" "$failure_reason" "$release_slug" "$target_sha"
  fi
}

fail() {
  # Failure detail remains private machine state. The runner only
  # accepts the fixed, token-only RELEASE_DEPLOY line below.
  printf '%s\n' 'ERROR: deployment failed' >&2
  emit_failure_marker
  exit 1
}

fail_with_status() {
  local exit_status="$1"
  printf '%s\n' 'ERROR: deployment failed' >&2
  emit_failure_marker
  exit "$exit_status"
}

validate_systemctl_binary() {
  [[ -f "$SYSTEMCTL_BIN" && ! -L "$SYSTEMCTL_BIN" ]] \
    || fail "trusted systemctl binary is unavailable"
  [[ "$(/usr/bin/stat -c '%F:%u:%g:%h:%a' -- "$SYSTEMCTL_BIN" 2>/dev/null)" \
    == "regular file:0:0:1:755" ]] \
    || fail "trusted systemctl binary metadata is unsafe"
  [[ "$(/usr/bin/readlink -f -- "$SYSTEMCTL_BIN" 2>/dev/null)" == "$SYSTEMCTL_BIN" ]] \
    || fail "trusted systemctl binary resolves through a link"
}

artifact_identity_snapshot() {
  local directory="$1" marker="$1/.old-sparky-platform-artifact-owner"
  local directory_metadata marker_metadata marker_content parent_metadata
  local directory_device directory_inode expected_marker_content
  [[ "$directory" =~ ^/tmp/old-sparky-platform-artifact-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}$ ]] \
    || return 1
  [[ -d "$directory" && ! -L "$directory" ]] || return 1
  [[ "$(/usr/bin/stat -c '%F:%u' -- / 2>/dev/null)" == "directory:0" ]] \
    || return 1
  [[ "$(/usr/bin/stat -c '%F:%u:%g:%a' -- /tmp 2>/dev/null)" == "directory:0:0:1777" ]] \
    || return 1
  directory_metadata="$(/usr/bin/stat -c '%F:%u:%g:%h:%a:%d:%i' -- "$directory" 2>/dev/null)" \
    || return 1
  [[ "$directory_metadata" =~ ^directory:0:0:[2-9][0-9]*:700:[0-9]+:[0-9]+$ ]] \
    || return 1
  directory_inode="${directory_metadata##*:}"
  directory_device="${directory_metadata%:*}"
  directory_device="${directory_device##*:}"
  expected_marker_content="platform_prepare_artifact_dir schema=1 dev=$directory_device ino=$directory_inode"
  parent_metadata="$(/usr/bin/stat -c '%F:%u:%g:%a:%d:%i' -- /tmp 2>/dev/null)" \
    || return 1
  [[ "$parent_metadata" == directory:0:0:1777:* ]] || return 1
  [[ -f "$marker" && ! -L "$marker" ]] || return 1
  marker_metadata="$(/usr/bin/stat -c '%F:%u:%g:%h:%a:%d:%i:%s' -- "$marker" 2>/dev/null)" \
    || return 1
  [[ "$marker_metadata" =~ ^"regular file":0:0:1:600:[0-9]+:[0-9]+:([1-9][0-9]*)$ ]] \
    || return 1
  marker_content="$(/usr/bin/cat -- "$marker" 2>/dev/null)" || return 1
  [[ "$marker_content" == "$expected_marker_content" ]] || return 1
  [[ "$(/usr/bin/stat -c '%F:%u:%g:%h:%a:%d:%i:%s' -- "$marker" 2>/dev/null)" \
    == "$marker_metadata" ]] || return 1
  [[ "$(/usr/bin/stat -c '%F:%u:%g:%h:%a:%d:%i' -- "$directory" 2>/dev/null)" \
    == "$directory_metadata" ]] || return 1
  printf '%s|%s|%s\n' "$directory_metadata" "$marker_metadata" "$parent_metadata"
}

cleanup() {
  local cleanup_rc=$?
  trap - EXIT
  set +e
  if (( artifact_cleanup_owned == 1 )); then
    local current_artifact_identity=""
    current_artifact_identity="$(artifact_identity_snapshot "$artifact_dir" 2>/dev/null || true)"
    if [[ -n "$current_artifact_identity" \
      && "$current_artifact_identity" == "$artifact_identity_owned" ]]; then
      rm -rf -- "$artifact_dir" || cleanup_rc=1
    else
      # The dispatcher-created directory was replaced, relinked or otherwise
      # lost its immutable identity.  Never broaden cleanup to a new path.
      cleanup_rc=1
    fi
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

validate_systemctl_binary

if [[ -e "$artifact_dir" || -L "$artifact_dir" ]]; then
  artifact_identity_before_lock="$(artifact_identity_snapshot "$artifact_dir" 2>/dev/null || true)"
  [[ -n "$artifact_identity_before_lock" ]] || invalid_input
fi

restart_web_and_wait() {
  run_systemctl restart deadlock-web || return 1
  for _ in $(seq 1 30); do
    if service_is_active deadlock-web \
      && curl --fail --silent --show-error --max-time 2 \
        http://127.0.0.1:3000/ >/dev/null; then
      return 0
    fi
    sleep 1
  done
  fail "deadlock-web did not recover after runtime profile"
}

restart_api_and_wait() {
  run_systemctl restart deadlock-api || return 1
  for _ in $(seq 1 30); do
    if service_is_active deadlock-api \
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

if (( $# == 8 )); then
  [[ "$host_tools_sha" =~ ^[0-9a-f]{40}$ ]] || invalid_input
  [[ "$host_manifest_sha" =~ ^[0-9a-f]{64}$ ]] || invalid_input
  [[ "$host_capabilities_sha" =~ ^[0-9a-f]{64}$ ]] || invalid_input
  [[ "$host_tools_dir" == "/opt/oldsparky/platform/shared/host-tools/$host_tools_sha" ]] || invalid_input
  host_tools_validation_status=0
  /usr/bin/python3.12 -I -B - \
    "$host_tools_dir" "$host_tools_sha" "$host_manifest_sha" "$host_capabilities_sha" \
    >/dev/null 2>&1 <<'PY' || host_tools_validation_status=$?
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

root, expected_generation, expected_manifest, expected_capabilities = sys.argv[1:]
root = Path(root)
expected_generation = str(expected_generation)
expected_manifest = str(expected_manifest)
expected_capabilities = str(expected_capabilities)
expected_files = {
    "platform_workflow_remote_dispatch.py",
    "platform_workflow_input_guard.py",
    "platform_prepare_artifact_dir.py",
    "platform_production_deploy_supervisor.sh",
    "platform_release_lock.sh",
    "platform_release_preflight.sh",
    "platform_validate_release_artifact.py",
    "platform_safe_env_exec.py",
    "platform_render_service_envs.py",
    "platform_validate_edge_policy.py",
    "platform_update_cloudflare_ips.py",
    "platform_configure_shared_env.py",
    "platform_storage_evidence_summary.py",
    "capabilities.txt",
}
if set(path.name for path in root.iterdir()) != expected_files | {"manifest.json"}:
    raise SystemExit(1)
def read_stable(path, mode):
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
        or before.st_uid != 0 or before.st_gid != 0 or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != mode or before.st_size > 512 * 1024
    ):
        raise SystemExit(1)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        data = bytearray()
        while len(data) <= 512 * 1024:
            chunk = os.read(fd, min(1024 * 1024, 512 * 1024 + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        final = os.fstat(fd)
        if (
            len(data) != final.st_size or opened.st_ino != final.st_ino
            or opened.st_dev != final.st_dev or opened.st_mode != final.st_mode
            or opened.st_mtime_ns != final.st_mtime_ns or opened.st_ctime_ns != final.st_ctime_ns
        ):
            raise SystemExit(1)
        return bytes(data)
    finally:
        os.close(fd)
manifest_bytes = read_stable(root / "manifest.json", 0o444)
capabilities_bytes = read_stable(root / "capabilities.txt", 0o444)
if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest:
    raise SystemExit(1)
if hashlib.sha256(capabilities_bytes).hexdigest() != expected_capabilities:
    raise SystemExit(1)
manifest = json.loads(manifest_bytes.decode("utf-8"))
if (
    manifest.get("schema") != 1
    or manifest.get("source_sha") != expected_generation
    or manifest.get("generation") != expected_generation
    or not isinstance(manifest.get("files"), list)
):
    raise SystemExit(1)
records = {record.get("path"): record for record in manifest["files"] if isinstance(record, dict)}
if set(records) != expected_files:
    raise SystemExit(1)
for name, record in records.items():
    mode = 0o444 if name == "capabilities.txt" else 0o555
    data = capabilities_bytes if name == "capabilities.txt" else read_stable(root / name, mode)
    if set(record) != {"path", "sha256", "mode"} or record["mode"] != mode:
        raise SystemExit(1)
    if hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise SystemExit(1)
PY
  if (( host_tools_validation_status != 0 )); then
    fail_with_status "$host_tools_validation_status"
  fi
fi

# Lock order is release -> retained-load.  Both locks use the shared pathname
# supervisors with util-linux `--close`, so this body and every candidate child
# have no release or retained-load lock FD.  Each supervised body revalidates
# the exact exclusive WRITE FLOCK owner in /proc/locks before mutation.
lock_helper="$host_tools_dir/platform_release_lock.sh"
set_lock_failure_context helper_metadata
if [[ ! -f "$lock_helper" || -L "$lock_helper" || ! -x "$lock_helper" ]]; then
  fail "the canonical release lock helper is missing or unsafe"
fi
ORIGINAL_ARGS=("$@")
# shellcheck source=/dev/null
source "$lock_helper"
set_lock_failure_context release_supervise
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
set_lock_failure_context release_open
platform_release_lock_open || fail "the canonical release lock could not be validated"
set_lock_failure_context retained_supervise
if platform_retained_load_lock_supervise "${ORIGINAL_ARGS[@]}"; then
  :
else
  lock_status=$?
  if [[ "$lock_status" -eq "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE" ]]; then
    fail "the retained-load lock supervisor could not be started"
  fi
  # A successfully acquired flock returns its callback body's status.  Let
  # ordinary child failures and their already-validated marker reach the
  # dispatcher without relabeling them as a retained-lock boundary failure.
  exit "$lock_status"
fi
if [[ "${PLATFORM_RETAINED_LOAD_LOCK_SUPERVISED:-}" != "1" ]]; then
  exit 0
fi
set_lock_failure_context retained_open
platform_retained_load_lock_open \
  || fail "the retained-load lock could not be opened or is already held"

if [[ "$deploy_mode" == "deploy" ]]; then
  set_failure_context artifact artifact artifact_missing
  [[ -n "$artifact_identity_before_lock" ]] \
    || fail "CI release artifact directory was not prepared by the dispatcher"
  artifact_identity_owned="$(artifact_identity_snapshot "$artifact_dir" 2>/dev/null || true)"
  [[ -n "$artifact_identity_owned" \
    && "$artifact_identity_owned" == "$artifact_identity_before_lock" ]] \
    || fail "CI release artifact directory identity changed before ownership"
  artifact_cleanup_owned=1
fi
trap cleanup EXIT

set_failure_context preflight preflight environment
test "$(id -u)" -eq 0 || fail "deployment user must be root"
initial_install=0
current_only_install=0
if [[ -L "$current" ]]; then
  if [[ -L "$runtime/previous" ]]; then
    :
  elif [[ ! -e "$runtime/previous" ]]; then
    current_only_install=1
  else
    fail "previous release pointer is unsafe"
  fi
elif [[ ! -e "$current" ]]; then
  [[ ! -e "$runtime/previous" && ! -L "$runtime/previous" ]] \
    || fail "first install cannot have a previous release"
  initial_install=1
else
  fail "current release pointer is unsafe"
fi

if (( initial_install == 0 )); then
  set_failure_context preflight preflight service_state
  for service in deadlock-api deadlock-worker deadlock-web; do
    service_is_active "$service" || fail "$service is not active before deployment"
  done

  set_failure_context preflight preflight nginx_config
  run_nginx_config_test >/dev/null 2>&1 \
    || fail "Nginx configuration validation failed or timed out"
fi

set_failure_context preflight preflight preflight_failed
preflight_previous_flag=(--require-previous)
active_revision_preflight_flag=()
if [[ "$deploy_mode" == "deploy" ]]; then
  active_revision_preflight_flag=(--defer-active-alembic-revision-check)
fi
if (( initial_install == 1 )); then
  preflight_previous_flag=(--allow-initial-install)
elif (( current_only_install == 1 )); then
  preflight_previous_flag=(--allow-no-previous)
fi
"$host_tools_dir/platform_release_preflight.sh" \
  "${preflight_previous_flag[@]}" \
  "${active_revision_preflight_flag[@]}" \
  --require-verified-backup \
  --require-edge-parity \
  --backup-max-age-hours 24 >/dev/null 2>/dev/null \
  || fail "production preflight failed"

if [[ "$deploy_mode" == "preflight" ]]; then
  printf 'RELEASE_DEPLOY schema=1 status=passed class=preflight release_slug=%s source_sha=%s\n' \
    "$release_slug" "$target_sha"
  exit 0
fi

[[ "$deploy_mode" == "deploy" ]] || fail "unsupported deployment mode"
if [[ -n "$baseline_identity_b64" ]]; then
  set_failure_context preflight preflight baseline_changed
  [[ "$baseline_identity_b64" =~ ^[A-Za-z0-9+/]{1,4096}={0,2}$ ]] \
    || fail "authenticated release baseline is malformed"
  /usr/bin/python3.12 -I -B \
    "$host_tools_dir/platform_workflow_remote_dispatch.py" \
    host-release-baseline-match "$baseline_identity_b64" >/dev/null 2>&1 \
    || fail "active release identity changed before deployment"
fi
set_failure_context artifact artifact artifact_missing
if [[ ! -d "$artifact_dir" || -L "$artifact_dir" ]]; then
  fail "CI release artifact directory is missing"
fi

if artifact_count="$(find "$artifact_dir" -maxdepth 1 -type f -name '*.tar.gz' -printf 'x\n' | wc -l)"; then
  :
else
  artifact_command_status=$?
  set_failure_context artifact artifact artifact_count_invalid
  fail_with_status "$artifact_command_status"
fi
if [[ "$artifact_count" != "1" ]]; then
  set_failure_context artifact artifact artifact_count_invalid
  fail "CI release artifact count is invalid"
fi
if artifact_path="$(find "$artifact_dir" -maxdepth 1 -type f -name '*.tar.gz' -print -quit)"; then
  :
else
  artifact_command_status=$?
  set_failure_context artifact artifact artifact_missing
  fail_with_status "$artifact_command_status"
fi
[[ -n "$artifact_path" ]] || fail "CI release artifact is missing"
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
(cd "$artifact_dir" && sha256sum -c "$(basename "$artifact_checksum")" >/dev/null) \
  || {
    set_failure_context artifact artifact checksum_mismatch
    fail "CI release artifact digest mismatch"
  }
if bootstrap_dir="$(mktemp -d /tmp/old-sparky-release-bootstrap.XXXXXX)"; then
  :
else
  artifact_command_status=$?
  set_failure_context artifact artifact validation_failed
  fail_with_status "$artifact_command_status"
fi
if chmod 0700 "$bootstrap_dir"; then
  :
else
  artifact_command_status=$?
  set_failure_context artifact artifact validation_failed
  fail_with_status "$artifact_command_status"
fi
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
  "${preflight_previous_flag[@]}" \
  --defer-active-alembic-revision-check \
  --require-verified-backup \
  --require-edge-parity \
  --backup-max-age-hours 24 >/dev/null 2>/dev/null \
  || fail "production preflight failed"

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
      service_output="$(run_systemctl show "$service" \
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
    run_systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if service_is_active deadlock-api \
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
    run_systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if service_is_active deadlock-api \
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
      run_systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if service_is_active deadlock-api \
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
    run_systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if service_is_active deadlock-api \
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
      run_systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if service_is_active deadlock-api \
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
    run_systemctl restart deadlock-web
    web_ready=false
    for _ in $(seq 1 30); do
      if service_is_active deadlock-web \
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
      run_systemctl restart deadlock-web
      web_ready=false
      for _ in $(seq 1 30); do
        if service_is_active deadlock-web \
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
    run_systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if service_is_active deadlock-api \
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
      run_systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if service_is_active deadlock-api \
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
    run_systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if service_is_active deadlock-api \
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
      run_systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if service_is_active deadlock-api \
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
    run_systemctl restart deadlock-api
    api_ready=false
    for _ in $(seq 1 30); do
      if service_is_active deadlock-api \
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
      run_systemctl restart deadlock-api
      api_ready=false
      for _ in $(seq 1 30); do
        if service_is_active deadlock-api \
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
