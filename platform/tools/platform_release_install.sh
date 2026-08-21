#!/usr/bin/env bash
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

SKIP_PYTHON_DEPS=0
SEED_ENV_FROM=""

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
    --help|-h)
      cat <<'EOF'
Usage: platform_release_install.sh [--skip-python-deps] [--seed-env-from <env_file>] <artifact.tar.gz> [app_dir]

Installs a verified, prebuilt platform release into the standard:
  <app_dir>/releases
  <app_dir>/current
  <app_dir>/previous
  <app_dir>/shared

The default Python path builds and verifies a fresh offline venv before an
atomic swap. --skip-python-deps is accepted only when the existing shared venv
already passes pip check and exactly matches the artifact freeze.

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
  echo "Usage: platform_release_install.sh [--skip-python-deps] [--seed-env-from <env_file>] <artifact.tar.gz> [app_dir]" >&2
  exit 1
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "Platform release installation requires root." >&2
  exit 1
fi

ARTIFACT_PATH="$1"
APP_DIR="${2:-${PLATFORM_APP_DIR:-/opt/oldsparky/platform}}"
TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TRANSACTION_TOOL="$TOOLS_DIR/platform_release_transaction.py"

if [[ ! -f "$ARTIFACT_PATH" || -L "$ARTIFACT_PATH" ]]; then
  echo "Artifact is missing or unsafe: $ARTIFACT_PATH" >&2
  exit 1
fi
ARTIFACT_PATH="$(readlink -f "$ARTIFACT_PATH")"
CHECKSUM_PATH="$ARTIFACT_PATH.sha256"
if [[ ! -f "$CHECKSUM_PATH" || -L "$CHECKSUM_PATH" ]]; then
  echo "Adjacent release checksum is missing or unsafe: $CHECKSUM_PATH" >&2
  exit 1
fi
if [[ "$APP_DIR" != /* ]]; then
  echo "Application directory must be an absolute path." >&2
  exit 1
fi

if [[ -n "$SEED_ENV_FROM" ]]; then
  if [[ ! -f "$SEED_ENV_FROM" || -L "$SEED_ENV_FROM" ]]; then
    echo "Seed env file is missing or unsafe: $SEED_ENV_FROM" >&2
    exit 1
  fi
  SEED_ENV_FROM="$(readlink -f "$SEED_ENV_FROM")"
  SEED_UID="$(stat -c %u "$SEED_ENV_FROM")"
  SEED_MODE="$(stat -c %a "$SEED_ENV_FROM")"
  if [[ "$SEED_UID" != "0" || $((8#$SEED_MODE & 8#022)) -ne 0 ]]; then
    echo "Seed env file ownership or permissions are unsafe." >&2
    exit 1
  fi
fi

case "$(basename "$ARTIFACT_PATH")" in
  *.tar.gz)
    RELEASE_SLUG="$(basename "$ARTIFACT_PATH" .tar.gz)"
    ;;
  *)
    echo "Artifact must end with .tar.gz: $ARTIFACT_PATH" >&2
    exit 1
    ;;
esac
if [[ ! "$RELEASE_SLUG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
  echo "Artifact release slug is unsafe." >&2
  exit 1
fi

if [[ -e "$APP_DIR" || -L "$APP_DIR" ]]; then
  if [[ ! -d "$APP_DIR" || -L "$APP_DIR" ]]; then
    echo "Application directory is unsafe: $APP_DIR" >&2
    exit 1
  fi
else
  install -d -o root -g root -m 0755 "$APP_DIR"
fi
APP_DIR="$(readlink -f "$APP_DIR")"
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
      echo "Release install directory is unsafe: $REQUIRED_DIR" >&2
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
    echo "Release install directory ownership or permissions are unsafe: $SAFE_DIR" >&2
    exit 1
  fi
done

exec {RELEASE_LOCK_FD}<"$SHARED_DIR"
if ! /usr/bin/flock -n "$RELEASE_LOCK_FD"; then
  echo "Another platform install or rollback operation holds the release lock." >&2
  exit 3
fi
TRANSACTION_STATE="$SHARED_DIR/.release-operation.json"
if [[ -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
  cat >&2 <<EOF
A pending platform release operation must be recovered before installation.
Run:
  platform/tools/platform_release_rollback.sh --recover-pending --app-dir "$APP_DIR"
Then retry the install command.
EOF
  exit 3
fi

if [[ -e "$RELEASE_DIR" || -L "$RELEASE_DIR" ]]; then
  echo "Release already exists: $RELEASE_DIR" >&2
  exit 1
fi

validate_installed_release_target() {
  local target="$1"
  local label="$2"
  if [[ -z "$target" || ! -d "$target" || -L "$target" \
    || "$(dirname "$target")" != "$RELEASES_DIR" \
    || ! "$(basename "$target")" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
    echo "$label does not name a contained installed release." >&2
    return 1
  fi
  local target_uid target_mode
  target_uid="$(stat -c %u "$target")"
  target_mode="$(stat -c %a "$target")"
  if [[ "$target_uid" != "0" || $((8#$target_mode & 8#022)) -ne 0 ]]; then
    echo "$label target ownership or permissions are unsafe." >&2
    return 1
  fi
}

PREVIOUS_TARGET=""
if [[ -e "$APP_DIR/current" || -L "$APP_DIR/current" ]]; then
  if [[ ! -L "$APP_DIR/current" || "$(stat -c %u "$APP_DIR/current")" != "0" ]]; then
    echo "Current release pointer is not a symlink." >&2
    exit 1
  fi
  PREVIOUS_TARGET="$(readlink -f "$APP_DIR/current" 2>/dev/null || true)"
  validate_installed_release_target "$PREVIOUS_TARGET" "Current release pointer"
fi
ORIGINAL_PREVIOUS_TARGET=""
if [[ -e "$APP_DIR/previous" || -L "$APP_DIR/previous" ]]; then
  if [[ ! -L "$APP_DIR/previous" || "$(stat -c %u "$APP_DIR/previous")" != "0" ]]; then
    echo "Previous release pointer is not a symlink." >&2
    exit 1
  fi
  ORIGINAL_PREVIOUS_TARGET="$(readlink -f "$APP_DIR/previous" 2>/dev/null || true)"
  validate_installed_release_target "$ORIGINAL_PREVIOUS_TARGET" "Previous release pointer"
fi

INSTALL_COMPLETE=0
RELEASE_EXTRACTED=0
CREATED_ENV=0
NEW_VENV_DIR=""
FREEZE_CHECK_FILE=""

remove_tree() {
  local target="$1"
  if [[ -n "$target" && -d "$target" && ! -L "$target" ]]; then
    chmod -R u+rwX "$target" 2>/dev/null || true
    rm -rf -- "$target"
  fi
}

cleanup_failed_install() {
  local cleanup_failed=0
  if [[ "$INSTALL_COMPLETE" -eq 1 ]]; then
    return
  fi
  if [[ -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
    if /usr/bin/python3 -I "$TRANSACTION_TOOL" recover --state "$TRANSACTION_STATE"; then
      RELEASE_EXTRACTED=0
      CREATED_ENV=0
      NEW_VENV_DIR=""
    else
      echo "CRITICAL: durable release transaction recovery failed; state was retained." >&2
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
}
trap cleanup_failed_install EXIT

/usr/bin/python3 -I "$TOOLS_DIR/platform_validate_release_artifact.py" \
  --artifact "$ARTIFACT_PATH" \
  --checksum "$CHECKSUM_PATH" \
  --release-slug "$RELEASE_SLUG" \
  --extract-to "$RELEASES_DIR"
RELEASE_EXTRACTED=1

/usr/bin/python3 -I "$TOOLS_DIR/platform_validate_wheelhouse.py" verify \
  --wheelhouse "$RELEASE_DIR/wheelhouse" \
  --requirements "$RELEASE_DIR/requirements-platform.txt" \
  --lock "$RELEASE_DIR/requirements-platform.lock.txt" \
  --freeze "$RELEASE_DIR/requirements-platform.freeze.txt"

if [[ ! -f "$RELEASE_DIR/apps/platform_web/.next/standalone/server.js" ]]; then
  echo "Installed release is missing the Next.js standalone server artifact." >&2
  exit 1
fi
if [[ ! -d "$RELEASE_DIR/apps/platform_web/.next/standalone/.next/static" ]]; then
  echo "Installed release is missing the Next.js standalone static assets." >&2
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
  echo "Shared env file is unsafe." >&2
  exit 1
fi
ENV_UID="$(stat -c %u "$SHARED_ENV_FILE")"
ENV_LINKS="$(stat -c %h "$SHARED_ENV_FILE")"
ENV_MODE="$(stat -c %a "$SHARED_ENV_FILE")"
if [[ "$ENV_UID" != "0" || "$ENV_LINKS" != "1" \
  || $((8#$ENV_MODE & 8#022)) -ne 0 ]]; then
  echo "Shared env file ownership or permissions are unsafe." >&2
  exit 1
fi
if getent group oldsparky-platform >/dev/null 2>&1; then
  chown root:oldsparky-platform "$SHARED_ENV_FILE"
  chmod 0640 "$SHARED_ENV_FILE"
else
  chmod 0600 "$SHARED_ENV_FILE"
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
  run_isolated_python "$venv_dir/bin/python" -I -m pip check
  run_isolated_python "$venv_dir/bin/python" -I -m pip freeze --all \
    | /usr/bin/sort >"$freeze_output"
  if ! /usr/bin/cmp -s \
    "$RELEASE_DIR/requirements-platform.freeze.txt" "$freeze_output"; then
    echo "Installed Python environment does not exactly match the artifact freeze." >&2
    return 1
  fi
}

relocate_venv_paths() {
  /usr/bin/python3 -I - "$1" "$2" <<'PY'
import os
from pathlib import Path
import stat
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
old_prefix = os.fsencode(source)
new_prefix = os.fsencode(destination)
paths = [source / "pyvenv.cfg"]
try:
    paths.extend(sorted(source.joinpath("bin").iterdir()))
except OSError as exc:
    raise SystemExit("fresh venv bin directory is unavailable") from exc
for path in paths:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SystemExit(f"fresh venv path is unavailable: {path.name}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        continue
    if metadata.st_uid != 0 or metadata.st_nlink != 1 or metadata.st_size > 4 * 1024 * 1024:
        raise SystemExit(f"fresh venv path metadata is unsafe: {path.name}")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SystemExit(f"fresh venv path changed during relocation: {path.name}")
        raw = b""
        while chunk := os.read(descriptor, 1024 * 1024):
            raw += chunk
        if old_prefix not in raw:
            continue
        if b"\x00" in raw:
            raise SystemExit(f"fresh venv binary embeds its temporary path: {path.name}")
        relocated = raw.replace(old_prefix, new_prefix)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        view = memoryview(relocated)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SystemExit(f"fresh venv path relocation failed: {path.name}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
}

FREEZE_CHECK_FILE="$(mktemp "$SHARED_DIR/.freeze-check-$RELEASE_SLUG.XXXXXX")"
if [[ -e "$SHARED_VENV_DIR" || -L "$SHARED_VENV_DIR" ]]; then
  if [[ ! -d "$SHARED_VENV_DIR" || -L "$SHARED_VENV_DIR" \
    || ! -x "$SHARED_VENV_DIR/bin/python" ]]; then
    echo "Existing shared Python venv is unsafe." >&2
    exit 1
  fi
  VENV_UID="$(stat -c %u "$SHARED_VENV_DIR")"
  VENV_MODE="$(stat -c %a "$SHARED_VENV_DIR")"
  if [[ "$VENV_UID" != "0" || $((8#$VENV_MODE & 8#022)) -ne 0 ]]; then
    echo "Existing shared Python venv ownership or permissions are unsafe." >&2
    exit 1
  fi
fi
if [[ "$SKIP_PYTHON_DEPS" -eq 1 ]]; then
  if [[ ! -x "$SHARED_VENV_DIR/bin/python" ]]; then
    echo "--skip-python-deps requires an existing shared Python venv." >&2
    exit 1
  fi
  verify_venv "$SHARED_VENV_DIR" "$FREEZE_CHECK_FILE"
else
  NEW_VENV_DIR="$(mktemp -d "$SHARED_DIR/.venv-install-$RELEASE_SLUG.XXXXXX")"
  /usr/bin/python3 -I -m venv "$NEW_VENV_DIR"
  shopt -s nullglob
  PIP_WHEELS=("$RELEASE_DIR"/wheelhouse/pip-26.1.2-*.whl)
  shopt -u nullglob
  if (( ${#PIP_WHEELS[@]} != 1 )); then
    echo "Release wheelhouse must contain exactly one pip==26.1.2 wheel." >&2
    exit 1
  fi
  run_isolated_python "$NEW_VENV_DIR/bin/python" -I -m pip install \
    --no-index \
    --no-deps \
    --force-reinstall \
    "${PIP_WHEELS[0]}"
  run_isolated_python "$NEW_VENV_DIR/bin/python" -I -m pip install \
    --no-index \
    --find-links "$RELEASE_DIR/wheelhouse" \
    --only-binary=:all: \
    --upgrade \
    --force-reinstall \
    --requirement "$RELEASE_DIR/requirements-platform.lock.txt"
  relocate_venv_paths "$NEW_VENV_DIR" "$SHARED_VENV_DIR"
  verify_venv "$NEW_VENV_DIR" "$FREEZE_CHECK_FILE"
  chmod 0755 "$NEW_VENV_DIR"
  rm -f -- "$FREEZE_CHECK_FILE"
  FREEZE_CHECK_FILE=""

  TRANSACTION_TRANSITION="create"
  if [[ -e "$SHARED_VENV_DIR" || -L "$SHARED_VENV_DIR" ]]; then
    TRANSACTION_TRANSITION="exchange"
    install -d -o root -g root -m 0700 "$VENV_ROLLBACK_DIR"
    if [[ -e "$VENV_ROLLBACK_SNAPSHOT_DIR" || -L "$VENV_ROLLBACK_SNAPSHOT_DIR" ]]; then
      echo "Release venv rollback snapshot already exists." >&2
      exit 1
    fi
    if [[ -n "$PREVIOUS_TARGET" ]]; then
      printf '%s\n' "$PREVIOUS_TARGET" >"$VENV_ROLLBACK_PREVIOUS_FILE"
      chmod 0600 "$VENV_ROLLBACK_PREVIOUS_FILE"
    fi
    printf 'snapshot\n' >"$VENV_ROLLBACK_TRANSITION_FILE"
    chmod 0600 "$VENV_ROLLBACK_TRANSITION_FILE"
  fi
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
      echo "Skip-dependency rollback metadata already exists." >&2
      exit 1
    fi
    printf '%s\n' "$PREVIOUS_TARGET" >"$VENV_ROLLBACK_PREVIOUS_FILE"
    printf 'unchanged\n' >"$VENV_ROLLBACK_TRANSITION_FILE"
    FREEZE_DIGEST="$(
      /usr/bin/sha256sum "$RELEASE_DIR/requirements-platform.freeze.txt"
    )"
    FREEZE_DIGEST="${FREEZE_DIGEST%% *}"
    if [[ ! "$FREEZE_DIGEST" =~ ^[0-9a-f]{64}$ ]]; then
      echo "Artifact freeze digest is invalid." >&2
      exit 1
    fi
    printf '%s\n' "$FREEZE_DIGEST" >"$VENV_ROLLBACK_FREEZE_FILE"
    chmod 0600 \
      "$VENV_ROLLBACK_PREVIOUS_FILE" \
      "$VENV_ROLLBACK_TRANSITION_FILE" \
      "$VENV_ROLLBACK_FREEZE_FILE"
  fi
  SKIP_TRANSACTION_PEER="$SHARED_DIR/.venv-install-$RELEASE_SLUG.none"
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
  if [[ "$CREATED_ENV" -eq 1 ]]; then
    TRANSACTION_CREATE_ARGS+=(--remove-env-on-recovery)
  fi
  /usr/bin/python3 -I "$TRANSACTION_TOOL" "${TRANSACTION_CREATE_ARGS[@]}"
  /usr/bin/python3 -I "$TRANSACTION_TOOL" phase \
    --state "$TRANSACTION_STATE" \
    --expected prepared \
    --phase venv-transitioned
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
/usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"
INSTALL_COMPLETE=1
trap - HUP INT TERM
trap - EXIT

cat <<EOF
Platform release installed.

Current release:
  $RELEASE_DIR
Shared env file:
  $SHARED_ENV_FILE
Shared venv:
  $SHARED_VENV_DIR

Next steps:
1. Review the shared env file and set production values if this is the first deploy.
2. Run migrations:
   cd "$APP_DIR/current" && PLATFORM_ENV_FILE="$SHARED_ENV_FILE" PLATFORM_PYTHON_BIN="$SHARED_VENV_DIR/bin/python" tools/platform_run_alembic.sh upgrade head
3. Install units and prepare release-specific writable paths:
   cd "$APP_DIR/current" && tools/platform_install_systemd_units.sh
4. Restart services:
   systemctl restart deadlock-api deadlock-worker deadlock-web
5. Verify readiness:
   curl -sS http://127.0.0.1:8010/api/v1/health/ready
   curl -sS http://127.0.0.1:3000/
EOF
