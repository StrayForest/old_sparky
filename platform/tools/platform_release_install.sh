#!/usr/bin/env bash
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

ORIGINAL_ARGS=("$@")
SKIP_PYTHON_DEPS=0
SEED_ENV_FROM=""
STAGE_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-python-deps)
      SKIP_PYTHON_DEPS=1
      shift
      ;;
    --seed-env-from)
      if [[ $# -lt 2 ]]; then
        echo "--seed-env-from requires a file path." >&2
        exit 1
      fi
      SEED_ENV_FROM="$2"
      shift 2
      ;;
    --stage-only)
      STAGE_ONLY=1
      shift
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_release_install.sh [--stage-only] [--skip-python-deps] [--seed-env-from <env_file>] <artifact.tar.gz> [app_dir]

Installs a verified, prebuilt platform release into the standard:
  <app_dir>/releases
  <app_dir>/current
  <app_dir>/previous
  <app_dir>/shared

The default Python path builds and verifies a fresh offline venv before an
atomic swap. A venv is reused automatically only after a fail-closed proof of
unchanged dependency inputs, runtime and installed-file integrity.

--stage-only leaves a durable transaction at the staged candidate without
changing current/previous. It is the only mode used by the end-to-end deploy
orchestrator. Without it, this low-level installer retains the legacy pointer
activation behavior for recovery/test compatibility; production deploys must
never call that mode directly.

The deploy orchestrator and direct invocations enter through the shared
pathname-form release-lock supervisor. Numeric lock descriptors are never
passed through the environment: util-linux `flock --close` treats a numeric
argument as a pathname and release bodies must not inherit lock FDs.

By default, app_dir is /opt/oldsparky/platform.
EOF
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "RELEASE_INSTALL status=failed class=argument release_slug=unavailable" >&2
  exit 1
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "RELEASE_INSTALL status=failed class=privilege release_slug=unavailable" >&2
  exit 1
fi

ARTIFACT_PATH="$1"
APP_DIR="${2:-${PLATFORM_APP_DIR:-/opt/oldsparky/platform}}"
TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TRANSACTION_TOOL="$TOOLS_DIR/platform_release_transaction.py"

if [[ ! -f "$ARTIFACT_PATH" || -L "$ARTIFACT_PATH" ]]; then
  echo "RELEASE_INSTALL status=failed class=artifact release_slug=unavailable" >&2
  exit 1
fi
ARTIFACT_PATH="$(readlink -f "$ARTIFACT_PATH")"
CHECKSUM_PATH="$ARTIFACT_PATH.sha256"
if [[ ! -f "$CHECKSUM_PATH" || -L "$CHECKSUM_PATH" ]]; then
  echo "RELEASE_INSTALL status=failed class=checksum release_slug=unavailable" >&2
  exit 1
fi
if [[ "$APP_DIR" != /* ]]; then
  echo "RELEASE_INSTALL status=failed class=argument release_slug=unavailable" >&2
  exit 1
fi
if [[ -n "$SEED_ENV_FROM" ]]; then
  if [[ ! -f "$SEED_ENV_FROM" || -L "$SEED_ENV_FROM" ]]; then
    echo "RELEASE_INSTALL status=failed class=environment release_slug=unavailable" >&2
    exit 1
  fi
  SEED_ENV_FROM="$(readlink -f "$SEED_ENV_FROM")"
  SEED_UID="$(stat -c %u "$SEED_ENV_FROM" 2>/dev/null)"
  SEED_MODE="$(stat -c %a "$SEED_ENV_FROM" 2>/dev/null)"
  if [[ "$SEED_UID" != "0" || $((8#$SEED_MODE & 8#022)) -ne 0 ]]; then
    echo "RELEASE_INSTALL status=failed class=environment release_slug=unavailable" >&2
    exit 1
  fi
fi

case "$(basename "$ARTIFACT_PATH")" in
  *.tar.gz)
    RELEASE_SLUG="$(basename "$ARTIFACT_PATH" .tar.gz)"
    ;;
  *)
    echo "RELEASE_INSTALL status=failed class=artifact release_slug=unavailable" >&2
    exit 1
    ;;
esac
if [[ ! "$RELEASE_SLUG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
  echo "RELEASE_INSTALL status=failed class=artifact release_slug=unavailable" >&2
  exit 1
fi

PUBLIC_RELEASE_SLUG="$RELEASE_SLUG"
public_status() {
  local status="$1"
  local class="$2"
  printf 'RELEASE_INSTALL schema=1 status=%s class=%s release_slug=%s\n' \
    "$status" "$class" "$PUBLIC_RELEASE_SLUG"
}

VENV_INSTALL_POLICY="legacy"

read_venv_install_policy() {
  local policy
  if ! policy="$(/usr/bin/python3 -I -B - "$RELEASE_DIR" <<'PY'
import json
import os
import stat
import sys

release_path = sys.argv[1]
maximum = 1024
directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
release_fd = -1
deploy_fd = -1
policy_fd = -1

def identity(value):
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_uid,
        value.st_gid, value.st_nlink, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )

try:
    release_before = os.lstat(release_path)
    if (not stat.S_ISDIR(release_before.st_mode) or release_before.st_uid != 0
            or release_before.st_gid != 0 or stat.S_IMODE(release_before.st_mode) & 0o022):
        raise SystemExit(1)
    release_fd = os.open(release_path, directory_flags)
    release_opened = os.fstat(release_fd)
    if identity(release_opened) != identity(release_before):
        raise SystemExit(1)
    try:
        deploy_fd = os.open("deploy", directory_flags, dir_fd=release_fd)
    except FileNotFoundError:
        print("legacy")
        raise SystemExit(0)
    deploy_before = os.fstat(deploy_fd)
    if (not stat.S_ISDIR(deploy_before.st_mode) or deploy_before.st_uid != 0
            or deploy_before.st_gid != 0 or stat.S_IMODE(deploy_before.st_mode) & 0o022):
        raise SystemExit(1)
    try:
        policy_fd = os.open(
            "python-venv-policy.json",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=deploy_fd,
        )
    except FileNotFoundError:
        print("legacy")
        raise SystemExit(0)
    before = os.fstat(policy_fd)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) != 0o644 or before.st_nlink != 1
            or before.st_size <= 0 or before.st_size > maximum):
        raise SystemExit(1)
    raw = bytearray()
    while len(raw) <= maximum:
        chunk = os.read(policy_fd, min(4096, maximum + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
    after = os.fstat(policy_fd)
    policy_path_after = os.stat(
        "python-venv-policy.json", dir_fd=deploy_fd, follow_symlinks=False
    )
    deploy_after = os.fstat(deploy_fd)
    release_after = os.fstat(release_fd)
    if (len(raw) != before.st_size or identity(after) != identity(before)
            or identity(policy_path_after) != identity(before)
            or identity(deploy_after) != identity(deploy_before)
            or identity(release_after) != identity(release_opened)):
        raise SystemExit(1)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if (not isinstance(payload, dict) or set(payload) != {"schema", "transition"}
            or type(payload.get("schema")) is not int or payload["schema"] != 1
            or payload.get("transition") != "require_proven_reuse"):
        raise SystemExit(1)
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1) from None
else:
    print("require_proven_reuse")
finally:
    for descriptor in (policy_fd, deploy_fd, release_fd):
        if descriptor >= 0:
            os.close(descriptor)
PY
)"; then
    return 1
  fi
  case "$policy" in
    legacy|require_proven_reuse) VENV_INSTALL_POLICY="$policy" ;;
    *) return 1 ;;
  esac
}

install_space_floor_ok() {
  local available="$1"
  local total="$2"
  [[ "$available" =~ ^[0-9]+$ && "$total" =~ ^[1-9][0-9]*$ ]] || return 1
  local filesystem_floor=$(((total * 15 + 99) / 100))
  local hard_floor=$((5 * 1024 * 1024 * 1024))
  local margin=$((128 * 1024 * 1024))
  (( filesystem_floor > hard_floor )) || filesystem_floor="$hard_floor"
  (( available >= filesystem_floor + margin ))
}

read_install_space() {
  /usr/bin/python3 -I -B - "$SHARED_DIR" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
try:
    before = os.lstat(path)
    if (not stat.S_ISDIR(before.st_mode) or before.st_uid != 0 or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) & 0o022):
        raise SystemExit(1)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        def identity(value):
            return (
                value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                value.st_gid, value.st_nlink,
            )

        if identity(opened) != identity(before):
            raise SystemExit(1)
        info = os.fstatvfs(fd)
        after = os.fstat(fd)
        current = os.lstat(path)
        if identity(after) != identity(opened) or identity(current) != identity(opened):
            raise SystemExit(1)
        available = info.f_bavail * info.f_frsize
        total = info.f_blocks * info.f_frsize
        if available < 0 or total <= 0:
            raise SystemExit(1)
        print(f"{available} {total}")
    finally:
        os.close(fd)
except OSError:
    raise SystemExit(1) from None
PY
}

enforce_required_venv_reuse() {
  if ! read_venv_install_policy; then
    return 1
  fi
  [[ "$VENV_INSTALL_POLICY" == "require_proven_reuse" ]] || return 0
  # A caller-supplied skip flag is never accepted for an artifact whose own
  # closed policy requires the strict verifier below.
  (( SKIP_PYTHON_DEPS == 0 )) || return 1
  [[ -n "$PREVIOUS_TARGET" && -x /usr/bin/python3.12 \
    && -f "$TOOLS_DIR/platform_verify_venv_reuse.py" \
    && ! -L "$TOOLS_DIR/platform_verify_venv_reuse.py" ]] || return 1
  /usr/bin/python3 -I -S -B "$TOOLS_DIR/platform_verify_venv_reuse.py" \
    --app "$APP_DIR" \
    --current "$PREVIOUS_TARGET" \
    --candidate "$RELEASE_DIR" \
    --venv "$SHARED_VENV_DIR" \
    --python /usr/bin/python3.12 \
    --transaction-state "$TRANSACTION_STATE" \
    --quiesce-state "$SHARED_DIR/.release-quiesce.json" \
    --previous-before "$ORIGINAL_PREVIOUS_TARGET" >/dev/null 2>/dev/null || return 1
  local space available total
  space="$(read_install_space)" || return 1
  read -r available total <<<"$space"
  install_space_floor_ok "$available" "$total" || return 1
  SKIP_PYTHON_DEPS=1
}

LOCK_HELPER="$TOOLS_DIR/platform_release_lock.sh"
if [[ ! -f "$LOCK_HELPER" || -L "$LOCK_HELPER" ]]; then
  echo "RELEASE_INSTALL status=failed class=lock release_slug=$PUBLIC_RELEASE_SLUG" >&2
  exit 3
fi
# The pathname-form supervisor owns the release lock before the first
# APP_DIR/releases/shared mutation.  A nested installer re-validates its live
# /usr/bin/flock parent; no numeric descriptor is inherited or accepted.
# shellcheck source=/dev/null
source "$LOCK_HELPER"
platform_release_lock_supervise "${ORIGINAL_ARGS[@]}" || {
  lock_status=$?
  if [[ "$lock_status" -eq "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE" ]]; then
    echo "RELEASE_INSTALL status=failed class=lock release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 3
  fi
  exit "$lock_status"
}
if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" != "1" ]]; then
  exit 0
fi
if ! platform_release_lock_open; then
  echo "RELEASE_INSTALL status=failed class=lock release_slug=$PUBLIC_RELEASE_SLUG" >&2
  exit 3
fi
trap platform_release_lock_close EXIT

if [[ -e "$APP_DIR" || -L "$APP_DIR" ]]; then
  if [[ ! -d "$APP_DIR" || -L "$APP_DIR" ]]; then
    echo "RELEASE_INSTALL status=failed class=layout release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 1
  fi
else
  install -d -o root -g root -m 0755 "$APP_DIR"
fi
APP_DIR="$(readlink -f "$APP_DIR")"
if [[ "$STAGE_ONLY" -eq 0 && "$APP_DIR" == "/opt/oldsparky/platform" ]]; then
  echo "RELEASE_INSTALL status=failed class=production_guard release_slug=$PUBLIC_RELEASE_SLUG" >&2
  exit 1
fi
RELEASES_DIR="$APP_DIR/releases"
SHARED_DIR="$APP_DIR/shared"
SHARED_VENV_DIR="$SHARED_DIR/venv"
SHARED_ENV_FILE="$SHARED_DIR/.env.platform"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_SLUG"
VENV_ROLLBACK_DIR="$RELEASE_DIR/.rollback"
VENV_ROLLBACK_SNAPSHOT_DIR="$VENV_ROLLBACK_DIR/shared-venv-before-install"
VENV_ROLLBACK_PREVIOUS_FILE="$VENV_ROLLBACK_DIR/previous-release"
VENV_ROLLBACK_TRANSITION_FILE="$VENV_ROLLBACK_DIR/venv-transition"
VENV_ROLLBACK_FREEZE_FILE="$VENV_ROLLBACK_DIR/shared-freeze.sha256"

for REQUIRED_DIR in "$RELEASES_DIR" "$SHARED_DIR"; do
  if [[ -e "$REQUIRED_DIR" || -L "$REQUIRED_DIR" ]]; then
    if [[ ! -d "$REQUIRED_DIR" || -L "$REQUIRED_DIR" ]]; then
      echo "RELEASE_INSTALL status=failed class=layout release_slug=$PUBLIC_RELEASE_SLUG" >&2
      exit 1
    fi
  else
    install -d -o root -g root -m 0755 "$REQUIRED_DIR"
  fi
done
for SAFE_DIR in "$APP_DIR" "$RELEASES_DIR" "$SHARED_DIR"; do
  SAFE_UID="$(stat -c %u "$SAFE_DIR")"
  SAFE_MODE="$(stat -c %a "$SAFE_DIR")"
  if [[ "$SAFE_UID" != "0" || $((8#$SAFE_MODE & 8#022)) -ne 0 ]]; then
    echo "RELEASE_INSTALL status=failed class=layout release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 1
  fi
done

TRANSACTION_STATE="$SHARED_DIR/.release-operation.json"
PREPARE_RECEIPT=0
if [[ -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
  pending_phase=""
  if pending_status="$(
    /usr/bin/python3 -I "$TRANSACTION_TOOL" status --state "$TRANSACTION_STATE" 2>/dev/null
  )"; then
    pending_phase="${pending_status#* }"
  fi
  if [[ "$pending_phase" == "quiesce-pending" ]]; then
    PREPARE_RECEIPT=1
  else
    echo "RELEASE_INSTALL status=failed class=pending_operation release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 3
  fi
fi

if [[ -e "$RELEASE_DIR" || -L "$RELEASE_DIR" ]]; then
  echo "RELEASE_INSTALL status=failed class=duplicate release_slug=$PUBLIC_RELEASE_SLUG" >&2
  exit 1
fi

validate_installed_release_target() {
  local target="$1"
  if [[ -z "$target" || ! -d "$target" || -L "$target" \
    || "$(dirname "$target")" != "$RELEASES_DIR" \
    || ! "$(basename "$target")" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
    echo "RELEASE_INSTALL status=failed class=layout release_slug=$PUBLIC_RELEASE_SLUG" >&2
    return 1
  fi
  local target_uid target_mode
  target_uid="$(stat -c %u "$target" 2>/dev/null)"
  target_mode="$(stat -c %a "$target" 2>/dev/null)"
  if [[ "$target_uid" != "0" || $((8#$target_mode & 8#022)) -ne 0 ]]; then
    echo "RELEASE_INSTALL status=failed class=layout release_slug=$PUBLIC_RELEASE_SLUG" >&2
    return 1
  fi
}

PREVIOUS_TARGET=""
if [[ -e "$APP_DIR/current" || -L "$APP_DIR/current" ]]; then
  if [[ ! -L "$APP_DIR/current" || "$(stat -c %u "$APP_DIR/current")" != "0" ]]; then
    echo "RELEASE_INSTALL status=failed class=pointer release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 1
  fi
  if ! PREVIOUS_TARGET="$(readlink -f "$APP_DIR/current" 2>/dev/null)"; then
    echo "RELEASE_INSTALL status=failed class=pointer release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 1
  fi
  validate_installed_release_target "$PREVIOUS_TARGET"
fi
ORIGINAL_PREVIOUS_TARGET=""
if [[ -e "$APP_DIR/previous" || -L "$APP_DIR/previous" ]]; then
  if [[ ! -L "$APP_DIR/previous" || "$(stat -c %u "$APP_DIR/previous")" != "0" ]]; then
    echo "RELEASE_INSTALL status=failed class=pointer release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 1
  fi
  if ! ORIGINAL_PREVIOUS_TARGET="$(readlink -f "$APP_DIR/previous" 2>/dev/null)"; then
    echo "RELEASE_INSTALL status=failed class=pointer release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 1
  fi
  validate_installed_release_target "$ORIGINAL_PREVIOUS_TARGET"
fi

INSTALL_COMPLETE=0
RELEASE_EXTRACTED=0
CREATED_ENV=0
NEW_VENV_DIR=""
FREEZE_CHECK_FILE=""

remove_tree() {
  local target="$1"
  if [[ -n "$target" && -d "$target" && ! -L "$target" ]]; then
    if ! chmod -R u+rwX "$target" 2>/dev/null; then
      echo "RELEASE_INSTALL status=failed class=cleanup release_slug=$PUBLIC_RELEASE_SLUG" >&2
      return 1
    fi
    if ! rm -rf -- "$target"; then
      echo "RELEASE_INSTALL status=failed class=cleanup release_slug=$PUBLIC_RELEASE_SLUG" >&2
      return 1
    fi
  fi
}

cleanup_failed_install() {
  local cleanup_failed=0
  if [[ "$INSTALL_COMPLETE" -eq 1 ]]; then
    platform_release_lock_close
    return
  fi
  if [[ "$PREPARE_RECEIPT" -eq 1 \
    && ( -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ) ]]; then
    # The deploy wrapper owns the pre-quiesce receipt and must perform the
    # recovery under its still-held release lock. Removing this state here
    # would leave stopped writers untracked if the stage process is killed or
    # fails after promoting the receipt.
    echo "RELEASE_INSTALL status=failed class=pending_operation release_slug=$PUBLIC_RELEASE_SLUG" >&2
    platform_release_lock_close
    return
  fi
  if [[ -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
    if /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --state "$TRANSACTION_STATE"; then
      RELEASE_EXTRACTED=0
      CREATED_ENV=0
      NEW_VENV_DIR=""
      # Transaction recovery restores the production pointer, but the
      # digest-bound live-QA generation has its own durable pointer. Reconcile
      # the restored release before removing the candidate so a failed
      # activation cannot leave QA bound to a non-active release.
      restored_current=""
      if [[ -L "$APP_DIR/current" ]]; then
        restored_current="$(readlink -f "$APP_DIR/current" 2>/dev/null || true)"
      fi
      if [[ -n "$restored_current" && "$(dirname "$restored_current")" == "$RELEASES_DIR" \
        && -f "$restored_current/tools/platform_live_qa_runtime_install.py" \
        && ! -L "$restored_current/tools/platform_live_qa_runtime_install.py" ]]; then
        if ! "$SHARED_VENV_DIR/bin/python" -I \
          "$restored_current/tools/platform_live_qa_runtime_install.py" \
          reconcile --app-dir "$APP_DIR" >/dev/null 2>/dev/null; then
          echo "RELEASE_INSTALL status=failed class=liveqa_runtime_recovery release_slug=$PUBLIC_RELEASE_SLUG" >&2
          cleanup_failed=1
        fi
      elif [[ -n "$restored_current" ]]; then
        echo "RELEASE_INSTALL status=failed class=liveqa_runtime_recovery release_slug=$PUBLIC_RELEASE_SLUG" >&2
        cleanup_failed=1
      fi
    else
      echo "RELEASE_INSTALL status=failed class=recovery release_slug=$PUBLIC_RELEASE_SLUG" >&2
      cleanup_failed=1
    fi
  fi
  if [[ "$cleanup_failed" -eq 0 ]]; then
    remove_tree "$NEW_VENV_DIR"
  fi
  if [[ "$cleanup_failed" -eq 0 && "$CREATED_ENV" -eq 1 \
    && -f "$SHARED_ENV_FILE" && ! -L "$SHARED_ENV_FILE" ]]; then
    rm -f -- "$SHARED_ENV_FILE"
  fi
  if [[ "$RELEASE_EXTRACTED" -eq 1 && "$cleanup_failed" -eq 0 ]]; then
    remove_tree "$RELEASE_DIR"
  fi
  if [[ -n "$FREEZE_CHECK_FILE" && -f "$FREEZE_CHECK_FILE" ]]; then
    rm -f -- "$FREEZE_CHECK_FILE"
  fi
  platform_release_lock_close
}
trap cleanup_failed_install EXIT

/usr/bin/python3 -I "$TOOLS_DIR/platform_validate_release_artifact.py" \
  --artifact "$ARTIFACT_PATH" \
  --checksum "$CHECKSUM_PATH" \
  --release-slug "$RELEASE_SLUG" \
  --extract-to "$RELEASES_DIR" >/dev/null 2>/dev/null
RELEASE_EXTRACTED=1

/usr/bin/python3 -I "$TOOLS_DIR/platform_validate_wheelhouse.py" verify \
  --wheelhouse "$RELEASE_DIR/wheelhouse" \
  --requirements "$RELEASE_DIR/requirements-platform.txt" \
  --lock "$RELEASE_DIR/requirements-platform.lock.txt" \
  --freeze "$RELEASE_DIR/requirements-platform.freeze.txt" >/dev/null 2>/dev/null

if ! enforce_required_venv_reuse; then
  public_status failed venv_policy
  exit 1
fi

if [[ ! -f "$RELEASE_DIR/apps/platform_web/.next/standalone/server.js" ]]; then
    echo "RELEASE_INSTALL status=failed class=artifact release_slug=$PUBLIC_RELEASE_SLUG" >&2
  exit 1
fi
if [[ ! -d "$RELEASE_DIR/apps/platform_web/.next/standalone/.next/static" ]]; then
    echo "RELEASE_INSTALL status=failed class=artifact release_slug=$PUBLIC_RELEASE_SLUG" >&2
  exit 1
fi

if [[ ! -f "$SHARED_ENV_FILE" ]]; then
  if [[ -n "$SEED_ENV_FROM" ]]; then
    install -o root -g root -m 0600 "$SEED_ENV_FROM" "$SHARED_ENV_FILE"
  else
    install -o root -g root -m 0600 \
      "$RELEASE_DIR/.env.platform.example" "$SHARED_ENV_FILE"
  fi
  CREATED_ENV=1
fi
if [[ -L "$SHARED_ENV_FILE" || ! -f "$SHARED_ENV_FILE" ]]; then
  echo "RELEASE_INSTALL status=failed class=environment release_slug=$PUBLIC_RELEASE_SLUG" >&2
  exit 1
fi
ENV_UID="$(stat -c %u "$SHARED_ENV_FILE")"
ENV_LINKS="$(stat -c %h "$SHARED_ENV_FILE")"
ENV_MODE="$(stat -c %a "$SHARED_ENV_FILE")"
if [[ "$ENV_UID" != "0" || "$ENV_LINKS" != "1" \
  || $((8#$ENV_MODE & 8#022)) -ne 0 ]]; then
  echo "RELEASE_INSTALL status=failed class=environment release_slug=$PUBLIC_RELEASE_SLUG" >&2
  exit 1
fi
# The canonical env contains database/session/R2 credentials. Keep it root-only
# for the entire install transaction; the service preparer later renders scoped
# copies without ever widening this file's permissions.
chown root:root "$SHARED_ENV_FILE"
chmod 0600 "$SHARED_ENV_FILE"

if [[ "$VENV_INSTALL_POLICY" == "require_proven_reuse" ]]; then
  INSTALL_SPACE="$(read_install_space)" || {
    public_status failed venv_policy
    exit 1
  }
  read -r INSTALL_AVAILABLE INSTALL_TOTAL <<<"$INSTALL_SPACE"
  if ! install_space_floor_ok "$INSTALL_AVAILABLE" "$INSTALL_TOTAL"; then
    public_status failed venv_policy
    exit 1
  fi
fi

run_isolated_python() {
  local python_bin="$1"
  shift
  /usr/bin/env -i \
    HOME=/nonexistent \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/usr/bin:/bin \
    PIP_CONFIG_FILE=/dev/null \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INDEX=1 \
    "$python_bin" "$@"
}

verify_venv() {
  local venv_dir="$1"
  local freeze_output="$2"
  run_isolated_python "$venv_dir/bin/python" -I -B -m pip check >/dev/null 2>/dev/null
  run_isolated_python "$venv_dir/bin/python" -I -B -m pip freeze --all \
    2>/dev/null | /usr/bin/sort >"$freeze_output"
  if ! /usr/bin/cmp -s \
    "$RELEASE_DIR/requirements-platform.freeze.txt" "$freeze_output"; then
    echo "RELEASE_INSTALL status=failed class=venv release_slug=$PUBLIC_RELEASE_SLUG" >&2
    return 1
  fi
}

relocate_venv_paths() {
  /usr/bin/python3 -I -S -B - "$1" "$2" "$3" <<'PY'
import base64
import csv
import hashlib
import importlib.util
import marshal
import os
from pathlib import Path
import py_compile
import re
import stat
import sys
import zipfile

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
wheelhouse = Path(sys.argv[3])
old_prefix = os.fsencode(source)
new_prefix = os.fsencode(destination)
cache_removals = []

def safe_bytes(path, *, mode, max_size):
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
            or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size > max_size):
        raise SystemExit(1)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SystemExit(1)
        chunks = []
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > max_size:
                raise SystemExit(1)
            chunks.append(chunk)
        return b"".join(chunks), metadata
    finally:
        os.close(descriptor)

if sys.implementation.name != "cpython" or not sys.implementation.cache_tag:
    raise SystemExit(1)
try:
    stage_python = source / "bin" / "python"
    stage_python_info = stage_python.lstat()
    config, _ = safe_bytes(source / "pyvenv.cfg", mode=0o644, max_size=1024 * 1024)
    version_match = re.search(rb"(?m)^version\s*=\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", config)
    expected_version = ".".join(str(part) for part in sys.version_info[:3]).encode("ascii")
    if (not stat.S_ISLNK(stage_python_info.st_mode)
            or stage_python.resolve(strict=True) != Path(sys.executable).resolve(strict=True)
            or version_match is None or version_match.group(1) != expected_version):
        raise SystemExit(1)
except (OSError, RuntimeError, ValueError):
    raise SystemExit(1) from None

def authenticate_wheel_script(script, installed_bytes):
    directory = wheelhouse.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode)
            or directory.st_uid != 0 or stat.S_IMODE(directory.st_mode) != 0o555):
        raise SystemExit(1)
    manifest_path = wheelhouse / "WHEELHOUSE.sha256"
    manifest, _ = safe_bytes(manifest_path, mode=0o444, max_size=1024 * 1024)
    hashes = {}
    for line in manifest.decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+!-]+\.whl)", line)
        if match is None or match.group(2) in hashes:
            raise SystemExit(1)
        hashes[match.group(2)] = match.group(1)
    wheel_files = sorted(wheelhouse.glob("*.whl"), key=lambda path: path.name)
    if not wheel_files or [path.name for path in wheel_files] != sorted(hashes):
        raise SystemExit(1)
    matches = []
    for wheel in wheel_files:
        wheel_bytes, wheel_info = safe_bytes(wheel, mode=0o444, max_size=256 * 1024 * 1024)
        if hashlib.sha256(wheel_bytes).hexdigest() != hashes[wheel.name]:
            raise SystemExit(1)
        try:
            with zipfile.ZipFile(wheel) as archive:
                record_names = [name for name in archive.namelist()
                                if name.endswith(".dist-info/RECORD")]
                if len(record_names) != 1:
                    raise SystemExit(1)
                record_rows = {}
                for row in csv.reader(archive.read(record_names[0]).decode("utf-8").splitlines()):
                    if len(row) != 3:
                        raise SystemExit(1)
                    record_rows.setdefault(row[0], []).append((row[1], row[2]))
                for member_name in archive.namelist():
                    parts = Path(member_name).parts
                    if (len(parts) < 3 or not parts[0].endswith(".data")
                            or parts[1] != "scripts" or parts[-1] != script.name
                            or ".." in parts):
                        continue
                    member = archive.read(member_name)
                    row_values = record_rows.get(member_name, [])
                    member_hash = base64.urlsafe_b64encode(
                        hashlib.sha256(member).digest()
                    ).rstrip(b"=").decode("ascii")
                    expected_installed = (
                        b"#!" + old_prefix + b"/bin/python"
                        + member[len(b"#!python"):]
                    ) if member.startswith(b"#!python") else b""
                    if (len(row_values) == 1
                            and row_values[0] == (f"sha256={member_hash}", str(len(member)))
                            and installed_bytes == expected_installed):
                        matches.append((wheel, member_name))
        except (OSError, UnicodeError, zipfile.BadZipFile, KeyError):
            raise SystemExit(1) from None
    if len(matches) != 1:
        raise SystemExit(1)
    return matches[0]

def cache_for_rewritten_script(script, installed_bytes):
    cache = Path(importlib.util.cache_from_source(str(script)))
    try:
        cache_info = cache.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(cache_info.st_mode) or cache_info.st_uid != 0
            or cache_info.st_nlink != 1 or stat.S_IMODE(cache_info.st_mode) != 0o644
            or cache_info.st_size > 16 * 1024 * 1024):
        raise SystemExit(1)
    cache_directory = cache.parent.lstat()
    if (not stat.S_ISDIR(cache_directory.st_mode) or stat.S_ISLNK(cache_directory.st_mode)
            or cache.parent != source / "bin" / "__pycache__"
            or cache_directory.st_uid != 0 or stat.S_IMODE(cache_directory.st_mode) != 0o755):
        raise SystemExit(1)
    cache_bytes, cache_info = safe_bytes(cache, mode=0o644, max_size=16 * 1024 * 1024)
    authenticate_wheel_script(script, installed_bytes)
    site_roots = sorted((source / "lib").glob("python*/site-packages"))
    if len(site_roots) != 1:
        raise SystemExit(1)
    site_root = site_roots[0]
    source_digest = base64.urlsafe_b64encode(
        hashlib.sha256(installed_bytes).digest()
    ).rstrip(b"=").decode("ascii")
    source_row = (f"sha256={source_digest}", str(len(installed_bytes)))
    cache_row = ("", "")
    source_relative = os.path.relpath(script, site_root).replace(os.sep, "/")
    cache_relative = os.path.relpath(cache, site_root).replace(os.sep, "/")
    source_rows = []
    cache_rows = []
    for record in sorted(site_root.glob("*.dist-info/RECORD")):
        raw_record, _ = safe_bytes(record, mode=0o644, max_size=16 * 1024 * 1024)
        for row in csv.reader(raw_record.decode("utf-8").splitlines()):
            if len(row) != 3:
                raise SystemExit(1)
            target = (site_root / row[0]).resolve(strict=False)
            canonical = os.path.relpath(target, site_root).replace(os.sep, "/")
            if row[0] != canonical:
                raise SystemExit(1)
            if target == script.resolve(strict=True):
                source_rows.append((row[0], (row[1], row[2])))
            if target == cache.resolve(strict=True):
                cache_rows.append((row[0], (row[1], row[2])))
    if (source_rows != [(source_relative, source_row)]
            or cache_rows != [(cache_relative, cache_row)]):
        raise SystemExit(1)
    if len(cache_bytes) < 16 or cache_bytes[:4] != importlib.util.MAGIC_NUMBER:
        raise SystemExit(1)
    flags = int.from_bytes(cache_bytes[4:8], "little")
    if flags == 0:
        if (int.from_bytes(cache_bytes[8:12], "little") != int(script.stat().st_mtime)
                or int.from_bytes(cache_bytes[12:16], "little") != len(installed_bytes)):
            raise SystemExit(1)
    elif flags == 3:
        if cache_bytes[8:16] != importlib.util.source_hash(installed_bytes):
            raise SystemExit(1)
    else:
        raise SystemExit(1)
    try:
        cached_code = marshal.loads(cache_bytes[16:])
        expected_code = compile(installed_bytes, str(script), "exec", dont_inherit=True)
    except (ValueError, EOFError, TypeError, SyntaxError):
        raise SystemExit(1) from None
    if cached_code != expected_code:
        raise SystemExit(1)
    return cache, cache_info, hashlib.sha256(cache_bytes).digest()

paths = [source / "pyvenv.cfg"]
try:
    paths.extend(sorted(source.joinpath("bin").iterdir()))
except OSError:
    raise SystemExit(1) from None
for path in paths:
    try:
        metadata = path.lstat()
    except OSError:
        raise SystemExit(1) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        continue
    if metadata.st_uid != 0 or metadata.st_nlink != 1 or metadata.st_size > 4 * 1024 * 1024:
        raise SystemExit(1)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SystemExit(1)
        raw = b""
        while chunk := os.read(descriptor, 1024 * 1024):
            raw += chunk
        if old_prefix not in raw:
            continue
        if b"\x00" in raw:
            raise SystemExit(1)
        if path.parent == source / "bin" and path.suffix == ".py":
            cache_proof = cache_for_rewritten_script(path, raw)
            if cache_proof is not None:
                cache_removals.append((path, raw, cache_proof))
        relocated = raw.replace(old_prefix, new_prefix)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        view = memoryview(relocated)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SystemExit(1)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
for script, original, (cache, cache_info, cache_digest) in cache_removals:
    expected_after = original.replace(old_prefix, new_prefix)
    if script.read_bytes() != expected_after:
        raise SystemExit(1)
    current_cache = cache.lstat()
    cache_bytes, checked = safe_bytes(cache, mode=0o644, max_size=16 * 1024 * 1024)
    if ((current_cache.st_dev, current_cache.st_ino) != (cache_info.st_dev, cache_info.st_ino)
            or (checked.st_dev, checked.st_ino) != (cache_info.st_dev, cache_info.st_ino)
            or hashlib.sha256(cache_bytes).digest() != cache_digest):
        raise SystemExit(1)
    try:
        py_compile.compile(
            str(script), cfile=str(cache),
            dfile=str(destination / script.relative_to(source)), doraise=True,
            optimize=0, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
        )
    except (OSError, py_compile.PyCompileError):
        raise SystemExit(1) from None
    os.chmod(cache, 0o644)
    regenerated, regenerated_info = safe_bytes(
        cache, mode=0o644, max_size=16 * 1024 * 1024
    )
    if (regenerated_info.st_uid != 0 or regenerated_info.st_nlink != 1
            or len(regenerated) < 16
            or regenerated[:4] != importlib.util.MAGIC_NUMBER
            or int.from_bytes(regenerated[4:8], "little") != 0
            or int.from_bytes(regenerated[8:12], "little") != int(script.stat().st_mtime)
            or int.from_bytes(regenerated[12:16], "little") != len(expected_after)):
        raise SystemExit(1)
    try:
        regenerated_code = marshal.loads(regenerated[16:])
        expected_code = compile(
            expected_after, str(destination / script.relative_to(source)),
            "exec", dont_inherit=True,
        )
    except (ValueError, EOFError, TypeError, SyntaxError):
        raise SystemExit(1) from None
    if regenerated_code != expected_code:
        raise SystemExit(1)
PY
}

# A prior successful transaction can prove the shared venv already contains
# the exact immutable dependencies and runtime required by this candidate.
# A proof miss deliberately follows the normal fresh-venv + full-snapshot path.
if [[ "$SKIP_PYTHON_DEPS" -eq 0 && -n "$PREVIOUS_TARGET" \
  && -x /usr/bin/python3.12 \
  && -f "$TOOLS_DIR/platform_verify_venv_reuse.py" \
  && ! -L "$TOOLS_DIR/platform_verify_venv_reuse.py" ]]; then
  if /usr/bin/python3 -I -S -B "$TOOLS_DIR/platform_verify_venv_reuse.py" \
    --app "$APP_DIR" \
    --current "$PREVIOUS_TARGET" \
    --candidate "$RELEASE_DIR" \
    --venv "$SHARED_VENV_DIR" \
    --python /usr/bin/python3.12 \
    --transaction-state "$TRANSACTION_STATE" \
    --quiesce-state "$SHARED_DIR/.release-quiesce.json" \
    --previous-before "$ORIGINAL_PREVIOUS_TARGET" >/dev/null 2>/dev/null; then
    SKIP_PYTHON_DEPS=1
  fi
fi

FREEZE_CHECK_FILE="$(mktemp "$SHARED_DIR/.freeze-check-$RELEASE_SLUG.XXXXXX")"
if [[ -e "$SHARED_VENV_DIR" || -L "$SHARED_VENV_DIR" ]]; then
  if [[ ! -d "$SHARED_VENV_DIR" || -L "$SHARED_VENV_DIR" \
    || ! -x "$SHARED_VENV_DIR/bin/python" ]]; then
    echo "RELEASE_INSTALL status=failed class=venv release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 1
  fi
  VENV_UID="$(stat -c %u "$SHARED_VENV_DIR")"
  VENV_MODE="$(stat -c %a "$SHARED_VENV_DIR")"
  if [[ "$VENV_UID" != "0" || $((8#$VENV_MODE & 8#022)) -ne 0 ]]; then
    echo "RELEASE_INSTALL status=failed class=venv release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 1
  fi
fi
if [[ "$SKIP_PYTHON_DEPS" -eq 1 ]]; then
  if [[ ! -x "$SHARED_VENV_DIR/bin/python" ]]; then
    echo "RELEASE_INSTALL status=failed class=venv release_slug=$PUBLIC_RELEASE_SLUG" >&2
    exit 1
  fi
  verify_venv "$SHARED_VENV_DIR" "$FREEZE_CHECK_FILE"
else
  NEW_VENV_DIR="$(mktemp -d "$SHARED_DIR/.venv-install-$RELEASE_SLUG.XXXXXX")"
  # API/worker import this root-owned environment as non-root service users.
  # The installer itself keeps umask 077 for receipts, temporary files and
  # secret environment data; apply a readable-code umask only while creating
  # the new venv and installing its artifact-bound packages.
  (
    umask 022
    /usr/bin/python3 -I -m venv "$NEW_VENV_DIR" >/dev/null 2>/dev/null
    shopt -s nullglob
    PIP_WHEELS=("$RELEASE_DIR"/wheelhouse/pip-*.whl)
    shopt -u nullglob
    if (( ${#PIP_WHEELS[@]} != 1 )); then
      echo "RELEASE_INSTALL status=failed class=wheelhouse release_slug=$PUBLIC_RELEASE_SLUG" >&2
      exit 1
    fi
    run_isolated_python "$NEW_VENV_DIR/bin/python" -I -m pip install \
      --no-index \
      --no-deps \
      --force-reinstall \
      "${PIP_WHEELS[0]}" >/dev/null 2>/dev/null
    run_isolated_python "$NEW_VENV_DIR/bin/python" -I -m pip install \
      --no-index \
      --find-links "$RELEASE_DIR/wheelhouse" \
      --only-binary=:all: \
      --upgrade \
      --force-reinstall \
      --require-hashes \
      --requirement "$RELEASE_DIR/requirements-platform.lock.txt" >/dev/null 2>/dev/null
  )
  relocate_venv_paths "$NEW_VENV_DIR" "$SHARED_VENV_DIR" "$RELEASE_DIR/wheelhouse" >/dev/null 2>/dev/null
  verify_venv "$NEW_VENV_DIR" "$FREEZE_CHECK_FILE"
  chmod 0755 "$NEW_VENV_DIR"
  rm -f -- "$FREEZE_CHECK_FILE"
  FREEZE_CHECK_FILE=""

  TRANSACTION_TRANSITION="create"
  if [[ -e "$SHARED_VENV_DIR" || -L "$SHARED_VENV_DIR" ]]; then
    TRANSACTION_TRANSITION="exchange"
    install -d -o root -g root -m 0700 "$VENV_ROLLBACK_DIR"
    if [[ -e "$VENV_ROLLBACK_SNAPSHOT_DIR" || -L "$VENV_ROLLBACK_SNAPSHOT_DIR" ]]; then
      echo "RELEASE_INSTALL status=failed class=rollback_metadata release_slug=$PUBLIC_RELEASE_SLUG" >&2
      exit 1
    fi
    if [[ -n "$PREVIOUS_TARGET" ]]; then
      printf '%s\n' "$PREVIOUS_TARGET" >"$VENV_ROLLBACK_PREVIOUS_FILE"
      chmod 0600 "$VENV_ROLLBACK_PREVIOUS_FILE"
    fi
    printf 'snapshot\n' >"$VENV_ROLLBACK_TRANSITION_FILE"
    chmod 0600 "$VENV_ROLLBACK_TRANSITION_FILE"
  fi
  if [[ "$PREPARE_RECEIPT" -eq 1 ]]; then
    TRANSACTION_CREATE_ARGS=(
      promote-quiesce
      --state "$TRANSACTION_STATE"
      --candidate-release "$RELEASE_DIR"
      --shared-venv "$SHARED_VENV_DIR"
      --peer "$NEW_VENV_DIR"
      --snapshot "$VENV_ROLLBACK_SNAPSHOT_DIR"
      --transition "$TRANSACTION_TRANSITION"
    )
  else
    TRANSACTION_CREATE_ARGS=(
      create
      --state "$TRANSACTION_STATE"
      --operation install
      --app-dir "$APP_DIR"
      --current-before "$PREVIOUS_TARGET"
      --previous-before "$ORIGINAL_PREVIOUS_TARGET"
      --candidate-release "$RELEASE_DIR"
      --shared-venv "$SHARED_VENV_DIR"
      --peer "$NEW_VENV_DIR"
      --snapshot "$VENV_ROLLBACK_SNAPSHOT_DIR"
      --transition "$TRANSACTION_TRANSITION"
    )
  fi
  if [[ "$CREATED_ENV" -eq 1 ]]; then
    TRANSACTION_CREATE_ARGS+=(--remove-env-on-recovery)
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" "${TRANSACTION_CREATE_ARGS[@]}"
  NEW_VENV_DIR=""
  if [[ "$TRANSACTION_TRANSITION" == "exchange" ]]; then
    trap '' HUP INT TERM
    /usr/bin/python3 -I "$TRANSACTION_TOOL" exchange --state "$TRANSACTION_STATE"
    /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
      --state "$TRANSACTION_STATE" \
      --expected prepared \
      --phase venv-transitioned
    /usr/bin/python3 -I "$TRANSACTION_TOOL" rename \
      --state "$TRANSACTION_STATE" \
      --mode place-snapshot
    /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
      --state "$TRANSACTION_STATE" \
      --expected venv-transitioned \
      --phase snapshot-placed
    trap - HUP INT TERM
  else
    trap '' HUP INT TERM
    /usr/bin/python3 -I "$TRANSACTION_TOOL" rename \
      --state "$TRANSACTION_STATE" \
      --mode activate-created
    /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
      --state "$TRANSACTION_STATE" \
      --expected prepared \
      --phase venv-transitioned
    trap - HUP INT TERM
  fi
fi

if [[ "$SKIP_PYTHON_DEPS" -eq 1 ]]; then
  rm -f -- "$FREEZE_CHECK_FILE"
  FREEZE_CHECK_FILE=""
  if [[ -n "$PREVIOUS_TARGET" ]]; then
    install -d -o root -g root -m 0700 "$VENV_ROLLBACK_DIR"
    if [[ -e "$VENV_ROLLBACK_PREVIOUS_FILE" \
      || -L "$VENV_ROLLBACK_PREVIOUS_FILE" \
      || -e "$VENV_ROLLBACK_TRANSITION_FILE" \
      || -L "$VENV_ROLLBACK_TRANSITION_FILE" \
      || -e "$VENV_ROLLBACK_FREEZE_FILE" \
      || -L "$VENV_ROLLBACK_FREEZE_FILE" ]]; then
      echo "RELEASE_INSTALL status=failed class=rollback_metadata release_slug=$PUBLIC_RELEASE_SLUG" >&2
      exit 1
    fi
    printf '%s\n' "$PREVIOUS_TARGET" >"$VENV_ROLLBACK_PREVIOUS_FILE"
    printf 'unchanged\n' >"$VENV_ROLLBACK_TRANSITION_FILE"
    FREEZE_DIGEST="$(
      /usr/bin/sha256sum "$RELEASE_DIR/requirements-platform.freeze.txt"
    )"
    FREEZE_DIGEST="${FREEZE_DIGEST%% *}"
    if [[ ! "$FREEZE_DIGEST" =~ ^[0-9a-f]{64}$ ]]; then
      echo "RELEASE_INSTALL status=failed class=artifact release_slug=$PUBLIC_RELEASE_SLUG" >&2
      exit 1
    fi
    printf '%s\n' "$FREEZE_DIGEST" >"$VENV_ROLLBACK_FREEZE_FILE"
    chmod 0600 \
      "$VENV_ROLLBACK_PREVIOUS_FILE" \
      "$VENV_ROLLBACK_TRANSITION_FILE" \
      "$VENV_ROLLBACK_FREEZE_FILE"
  fi
  SKIP_TRANSACTION_PEER="$SHARED_DIR/.venv-install-$RELEASE_SLUG.none"
  if [[ "$PREPARE_RECEIPT" -eq 1 ]]; then
    TRANSACTION_CREATE_ARGS=(
      promote-quiesce
      --state "$TRANSACTION_STATE"
      --candidate-release "$RELEASE_DIR"
      --shared-venv "$SHARED_VENV_DIR"
      --peer "$SKIP_TRANSACTION_PEER"
      --snapshot "$VENV_ROLLBACK_SNAPSHOT_DIR"
      --transition none
    )
  else
    TRANSACTION_CREATE_ARGS=(
      create
      --state "$TRANSACTION_STATE"
      --operation install
      --app-dir "$APP_DIR"
      --current-before "$PREVIOUS_TARGET"
      --previous-before "$ORIGINAL_PREVIOUS_TARGET"
      --candidate-release "$RELEASE_DIR"
      --shared-venv "$SHARED_VENV_DIR"
      --peer "$SKIP_TRANSACTION_PEER"
      --snapshot "$VENV_ROLLBACK_SNAPSHOT_DIR"
      --transition none
    )
  fi
  if [[ "$CREATED_ENV" -eq 1 ]]; then
    TRANSACTION_CREATE_ARGS+=(--remove-env-on-recovery)
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" "${TRANSACTION_CREATE_ARGS[@]}"
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected prepared \
    --phase venv-transitioned
fi

if [[ "$STAGE_ONLY" -eq 1 ]]; then
  # Install units and prepare release-specific writable paths before activation.
  trap '' HUP INT TERM
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected "$([[ "$SKIP_PYTHON_DEPS" -eq 0 && "${TRANSACTION_TRANSITION:-}" == "exchange" ]] && echo snapshot-placed || echo venv-transitioned)" \
    --phase staged
  trap - HUP INT TERM
  INSTALL_COMPLETE=1
  platform_release_lock_close
  trap - EXIT
  public_status passed staged
  exit 0
fi

POINTER_PHASE="venv-transitioned"
if [[ "$SKIP_PYTHON_DEPS" -eq 0 && "${TRANSACTION_TRANSITION:-}" == "exchange" ]]; then
  POINTER_PHASE="snapshot-placed"
fi
if [[ -n "$PREVIOUS_TARGET" && "$PREVIOUS_TARGET" != "$RELEASE_DIR" ]]; then
  trap '' HUP INT TERM
  /usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
    --state "$TRANSACTION_STATE" \
    --name previous \
    --target "$PREVIOUS_TARGET"
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected "$POINTER_PHASE" \
    --phase previous-switched
  trap - HUP INT TERM
  POINTER_PHASE="previous-switched"
fi
trap '' HUP INT TERM
/usr/bin/python3 -I "$TRANSACTION_TOOL" switch-pointer \
  --state "$TRANSACTION_STATE" \
  --name current \
  --target "$RELEASE_DIR"
/usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
  --state "$TRANSACTION_STATE" \
  --expected "$POINTER_PHASE" \
  --phase pointers-switched
# The active release and the trusted live-QA generation are one release
# identity.  Reconcile while the canonical release lock is still held; a
# failure leaves the transaction retained for guarded pointer/runtime recovery.
LIVE_QA_RUNTIME_INSTALLER="$RELEASE_DIR/tools/platform_live_qa_runtime_install.py"
if [[ ! -f "$LIVE_QA_RUNTIME_INSTALLER" || -L "$LIVE_QA_RUNTIME_INSTALLER" ]]; then
  echo "RELEASE_INSTALL status=failed class=liveqa_runtime release_slug=$PUBLIC_RELEASE_SLUG" >&2
  exit 1
fi
"$SHARED_VENV_DIR/bin/python" -I "$LIVE_QA_RUNTIME_INSTALLER" \
  reconcile --app-dir "$APP_DIR" >/dev/null
/usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"
INSTALL_COMPLETE=1
platform_release_lock_close
trap - HUP INT TERM
trap - EXIT

public_status installed installed
