#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUTPUT_DIR="${PLATFORM_RELEASE_OUTPUT_DIR:-$ROOT_DIR/dist/releases}"
WEB_NEXT_COMPRESSION="${PLATFORM_WEB_NEXT_COMPRESSION:-true}"
DEPENDENCY_BASELINE=""
RELEASE_REF_RAW="workspace"
RELEASE_REF_SET=0
CURRENT_PHASE="canonical-preflight"
FAILURE_REASON="build_failed"
ORIGINAL_RC=0
TELEMETRY_ENABLED=1
RELEASE_REF=""
BUILD_TIMESTAMP=""
RELEASE_SLUG=""
RELEASE_DIR=""
ARTIFACT_PATH=""
ARTIFACT_SHA_PATH=""
STAGING_DIR=""
ARTIFACT_TEMP=""
CHECKSUM_TEMP=""
RELEASE_ID=""
ARTIFACT_ID=""
CHECKSUM_ID=""
BUILD_COMPLETE=0
SOURCE_GIT_COMMIT=""
ARTIFACT_SHA256=""
PHASE_LOG_PATH="${PLATFORM_RELEASE_PHASE_LOG:-}"
PHASE_TELEMETRY_WRITER="$ROOT_DIR/tools/platform_release_phase_telemetry.py"
PHASE_TELEMETRY_ENABLED=0

setup_phase_telemetry() {
  if [[ -z "$PHASE_LOG_PATH" ]]; then
    return 0
  fi
  if [[ "$PHASE_LOG_PATH" != /* || "$PHASE_LOG_PATH" == *$'\n'* \
    || "$PHASE_LOG_PATH" == *$'\r'* || "$PHASE_LOG_PATH" == *$'\t'* \
    || ! -f "$PHASE_TELEMETRY_WRITER" || -L "$PHASE_TELEMETRY_WRITER" ]]; then
    return 1
  fi
  if ! /usr/bin/python3 -I "$PHASE_TELEMETRY_WRITER" \
    --check --marker-log "$PHASE_LOG_PATH" >/dev/null 2>&1; then
    return 1
  fi
  PHASE_TELEMETRY_ENABLED=1
}

emit_phase_marker() {
  local phase="$1"
  local status="$2"
  local reason="$3"
  local cleanup="$4"
  local failed_phase="${5:-}"
  local marker=""
  case "$reason" in
    ok|build_failed|cleanup_failed|interrupted) ;;
    *) reason="build_failed" ;;
  esac
  case "$cleanup" in
    not-run|passed|failed) ;;
    *) cleanup="not-run" ;;
  esac
  if [[ "$phase" == "complete" && "$status" == "passed" ]]; then
    marker="RELEASE_BUILD_PHASE schema=1 phase=complete status=passed reason=ok cleanup=passed source_sha=$SOURCE_GIT_COMMIT artifact_sha256=$ARTIFACT_SHA256"
  elif [[ "$status" == "failed" ]]; then
    marker="RELEASE_BUILD_PHASE schema=1 phase=$phase status=failed reason=$reason cleanup=$cleanup failed_phase=${failed_phase:-$CURRENT_PHASE}"
  else
    marker="RELEASE_BUILD_PHASE schema=1 phase=$phase status=passed reason=ok cleanup=$cleanup"
  fi
  if [[ "$PHASE_TELEMETRY_ENABLED" -eq 1 ]] && ! /usr/bin/python3 -I \
    "$PHASE_TELEMETRY_WRITER" --append --marker-log "$PHASE_LOG_PATH" --marker "$marker" \
    >/dev/null 2>&1; then
    return 1
  fi
  printf '%s\n' "$marker" || true
}

mark_phase_passed() {
  emit_phase_marker "$CURRENT_PHASE" passed ok not-run
}

capture_error() {
  local rc="$1"
  if [[ "$ORIGINAL_RC" -eq 0 ]]; then
    ORIGINAL_RC="$rc"
  fi
  return 0
}

interrupt_build() {
  FAILURE_REASON="interrupted"
  exit "$1"
}

path_identity() {
  /usr/bin/stat -c '%d:%i' -- "$1"
}

cleanup_staging() {
  local rc=$?
  local cleanup_rc=0
  trap - ERR EXIT INT TERM HUP
  if [[ "$ORIGINAL_RC" -ne 0 ]]; then
    rc="$ORIGINAL_RC"
  fi
  set +e
  if [[ "$TELEMETRY_ENABLED" -eq 0 ]]; then
    exit "$rc"
  fi
  if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" && ! -L "$STAGING_DIR" ]]; then
    chmod -R u+rwX "$STAGING_DIR" 2>/dev/null || cleanup_rc=1
    rm -rf -- "$STAGING_DIR" || cleanup_rc=1
  fi
  for TEMP_FILE in "$ARTIFACT_TEMP" "$CHECKSUM_TEMP"; do
    if [[ -n "$TEMP_FILE" && -f "$TEMP_FILE" && ! -L "$TEMP_FILE" \
      && "$(dirname "$TEMP_FILE")" == "$OUTPUT_DIR" ]]; then
      rm -f -- "$TEMP_FILE" || cleanup_rc=1
    fi
  done
  if [[ "$BUILD_COMPLETE" -eq 0 ]]; then
    if [[ -n "$RELEASE_ID" && -d "$RELEASE_DIR" && ! -L "$RELEASE_DIR" \
      && "$(path_identity "$RELEASE_DIR")" == "$RELEASE_ID" ]]; then
      chmod -R u+rwX "$RELEASE_DIR" 2>/dev/null || cleanup_rc=1
      rm -rf -- "$RELEASE_DIR" || cleanup_rc=1
    fi
    if [[ -n "$ARTIFACT_ID" && -f "$ARTIFACT_PATH" && ! -L "$ARTIFACT_PATH" \
      && "$(path_identity "$ARTIFACT_PATH")" == "$ARTIFACT_ID" ]]; then
      rm -f -- "$ARTIFACT_PATH" || cleanup_rc=1
    fi
    if [[ -n "$CHECKSUM_ID" && -f "$ARTIFACT_SHA_PATH" \
      && ! -L "$ARTIFACT_SHA_PATH" \
      && "$(path_identity "$ARTIFACT_SHA_PATH")" == "$CHECKSUM_ID" ]]; then
      rm -f -- "$ARTIFACT_SHA_PATH" || cleanup_rc=1
    fi
  fi
  if [[ "$cleanup_rc" -ne 0 ]]; then
    if ! emit_phase_marker cleanup failed cleanup_failed failed "$CURRENT_PHASE"; then
      cleanup_rc=1
    fi
    if ! emit_phase_marker complete failed cleanup_failed failed "$CURRENT_PHASE"; then
      cleanup_rc=1
    fi
    [[ "$rc" -ne 0 ]] || rc=1
  else
    if ! emit_phase_marker cleanup passed ok passed; then
      cleanup_rc=1
    fi
    if [[ "$rc" -eq 0 && "$BUILD_COMPLETE" -eq 1 ]]; then
      if ! emit_phase_marker complete passed ok passed; then
        cleanup_rc=1
      fi
    else
      if ! emit_phase_marker complete failed "${FAILURE_REASON:-build_failed}" passed "$CURRENT_PHASE"; then
        cleanup_rc=1
      fi
    fi
    if [[ "$cleanup_rc" -ne 0 ]]; then
      [[ "$rc" -ne 0 ]] || rc=1
    fi
  fi
  exit "$rc"
}

trap 'capture_error "$?"' ERR
trap cleanup_staging EXIT
trap 'interrupt_build 130' INT
trap 'interrupt_build 143' TERM

case "$WEB_NEXT_COMPRESSION" in
  true|false) ;;
  *)
    echo "PLATFORM_WEB_NEXT_COMPRESSION must be true or false." >&2
    exit 1
    ;;
esac
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dependency-baseline)
      if [[ $# -lt 2 || -n "$DEPENDENCY_BASELINE" ]]; then
        echo "--dependency-baseline requires exactly one absolute release directory." >&2
        exit 1
      fi
      DEPENDENCY_BASELINE="$2"
      shift 2
      ;;
    --help|-h)
      TELEMETRY_ENABLED=0
      cat <<'EOF'
Usage: platform_build_release.sh [--dependency-baseline <absolute candidate-release-dir>] [release-ref]

Candidate builds omit --dependency-baseline. Enforcement builds must name the
candidate release directory in the same output root; all Python requirements,
resolved freeze, wheelhouse manifest, and web package lock bytes must match.
EOF
      exit 0
      ;;
    --*)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
    *)
      if [[ "$RELEASE_REF_SET" -eq 1 ]]; then
        echo "Only one release ref may be provided." >&2
        exit 1
      fi
      RELEASE_REF_RAW="$1"
      RELEASE_REF_SET=1
      shift
      ;;
  esac
done
if [[ ! "$RELEASE_REF_RAW" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ ]]; then
  echo "Release ref must be 1-100 safe slug characters and start with an alphanumeric." >&2
  exit 1
fi
if [[ "$OUTPUT_DIR" != /* ]]; then
  echo "Release output directory must be an absolute path." >&2
  exit 1
fi
if [[ -n "$DEPENDENCY_BASELINE" && "$DEPENDENCY_BASELINE" != /* ]]; then
  echo "Dependency baseline must be an absolute release directory." >&2
  exit 1
fi
if ! setup_phase_telemetry; then
  echo "Release phase telemetry setup failed." >&2
  exit 1
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "Platform release builds require root." >&2
  exit 1
fi
BOOTSTRAP_TOOL="$ROOT_DIR/tools/platform_bootstrap.sh"
if [[ ! -f "$BOOTSTRAP_TOOL" || -L "$BOOTSTRAP_TOOL" || ! -x "$BOOTSTRAP_TOOL" ]]; then
  echo "Release build prerequisite is missing: platform/tools/platform_bootstrap.sh" >&2
  exit 1
fi
if [[ ! -x "$ROOT_DIR/.venv_platform/bin/python" ]]; then
  echo "Missing platform/.venv_platform. Run platform/tools/platform_bootstrap.sh first." >&2
  exit 1
fi
RELEASE_REF="$RELEASE_REF_RAW"
BUILD_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RELEASE_SLUG="${RELEASE_REF}-${BUILD_TIMESTAMP}"
RELEASE_DIR="$OUTPUT_DIR/$RELEASE_SLUG"
ARTIFACT_PATH="$OUTPUT_DIR/$RELEASE_SLUG.tar.gz"
ARTIFACT_SHA_PATH="$ARTIFACT_PATH.sha256"
if [[ "$(readlink -m "$OUTPUT_DIR")" != "$OUTPUT_DIR" ]]; then
  echo "Release output directory is not canonical." >&2
  exit 1
fi
OUTPUT_PARENT="$(dirname "$OUTPUT_DIR")"
if [[ ! -d "$OUTPUT_PARENT" || -L "$OUTPUT_PARENT" \
  || "$(readlink -f "$OUTPUT_PARENT")" != "$OUTPUT_PARENT" ]]; then
  echo "Release output parent directory is unsafe." >&2
  exit 1
fi
OUTPUT_PARENT_UID="$(stat -c %u "$OUTPUT_PARENT")"
OUTPUT_PARENT_MODE="$(stat -c %a "$OUTPUT_PARENT")"
if [[ "$OUTPUT_PARENT_UID" != "0" || $((8#$OUTPUT_PARENT_MODE & 8#022)) -ne 0 ]]; then
  echo "Release output parent ownership or permissions are unsafe." >&2
  exit 1
fi
if [[ ! -e "$OUTPUT_DIR" ]]; then
  install -d -o root -g root -m 0755 "$OUTPUT_DIR"
fi
if [[ ! -d "$OUTPUT_DIR" || -L "$OUTPUT_DIR" \
  || "$(readlink -f "$OUTPUT_DIR")" != "$OUTPUT_DIR" ]]; then
  echo "Release output directory is unsafe." >&2
  exit 1
fi
OUTPUT_UID="$(stat -c %u "$OUTPUT_DIR")"
OUTPUT_MODE="$(stat -c %a "$OUTPUT_DIR")"
if [[ "$OUTPUT_UID" != "0" || $((8#$OUTPUT_MODE & 8#022)) -ne 0 ]]; then
  echo "Release output directory ownership or permissions are unsafe." >&2
  exit 1
fi
exec {BUILD_LOCK_FD}<"$OUTPUT_DIR"
if ! /usr/bin/flock -n "$BUILD_LOCK_FD"; then
  echo "Another platform release build holds the output lock." >&2
  exit 3
fi
if [[ -e "$RELEASE_DIR" || -L "$RELEASE_DIR" || -e "$ARTIFACT_PATH" \
  || -L "$ARTIFACT_PATH" || -e "$ARTIFACT_SHA_PATH" || -L "$ARTIFACT_SHA_PATH" ]]; then
  echo "Release output already exists for slug: $RELEASE_SLUG" >&2
  exit 1
fi

validate_dependency_baseline() {
  /usr/bin/python3 -I - "$1" "$OUTPUT_DIR" <<'PY'
from pathlib import Path
import os
import re
import stat
import sys


baseline = Path(sys.argv[1])
output = Path(sys.argv[2])
slug = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
required = (
    Path("requirements-platform.txt"),
    Path("requirements-platform.lock.txt"),
    Path("requirements-platform.freeze.txt"),
    Path("wheelhouse/WHEELHOUSE.sha256"),
    Path("apps/platform_web/package-lock.json"),
)


def safe_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"Dependency baseline {label} is unavailable.") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or resolved != path
    ):
        raise SystemExit(f"Dependency baseline {label} metadata is unsafe.")


if not baseline.is_absolute() or Path(os.path.abspath(baseline)) != baseline:
    raise SystemExit("Dependency baseline path is not canonical.")
safe_directory(output, "output root")
safe_directory(baseline, "release directory")
if baseline.parent != output or slug.fullmatch(baseline.name) is None:
    raise SystemExit("Dependency baseline must be a direct release in the output root.")
for relative in required:
    path = baseline / relative
    parent = path.parent
    while True:
        safe_directory(parent, f"directory {parent.relative_to(baseline)}")
        if parent == baseline:
            break
        parent = parent.parent
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"Dependency baseline file is unavailable: {relative}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or resolved != path
    ):
        raise SystemExit(f"Dependency baseline file metadata is unsafe: {relative}")
print(baseline)
PY
}

BASELINE_ID=""
if [[ -n "$DEPENDENCY_BASELINE" ]]; then
  DEPENDENCY_BASELINE="$(validate_dependency_baseline "$DEPENDENCY_BASELINE")"
  BASELINE_ID="$(path_identity "$DEPENDENCY_BASELINE")"
fi

REPO_ROOT="$(git -C "$ROOT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" || "$ROOT_DIR" != "$REPO_ROOT/platform" ]]; then
  echo "Release build must run from the tracked platform checkout." >&2
  exit 1
fi
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all -- platform)" ]]; then
  echo "Release build refused: platform has tracked or untracked source changes." >&2
  exit 1
fi
SOURCE_GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify HEAD)"
if [[ ! "$SOURCE_GIT_COMMIT" =~ ^[0-9a-f]{40,64}$ ]]; then
  echo "Release build refused: source HEAD is invalid." >&2
  exit 1
fi
mark_phase_passed
CURRENT_PHASE="node-runtime"

EXPECTED_NODE_VERSION="26.3.1"
EXPECTED_NPM_VERSION="11.16.0"
PINNED_NODE_HOME="$(
  /usr/bin/python3 -I "$ROOT_DIR/tools/platform_live_qa_guard.py" prepare-build-node
)"
if [[ ! -x "$PINNED_NODE_HOME/bin/node" || ! -x "$PINNED_NODE_HOME/bin/npm" ]]; then
  echo "Pinned root-controlled Node 26 runtime is unavailable." >&2
  exit 1
fi
export PLATFORM_NODE_BIN="$PINNED_NODE_HOME/bin/node"

NODE_VERSION="$(
  /usr/bin/env -i \
    HOME=/nonexistent \
    LANG=C.UTF-8 \
    PATH="$PINNED_NODE_HOME/bin:/usr/bin:/bin" \
    "$PLATFORM_NODE_BIN" -p "process.versions.node"
)"
if [[ "$NODE_VERSION" != "$EXPECTED_NODE_VERSION" ]]; then
  echo "Release builds require Node $EXPECTED_NODE_VERSION; got $NODE_VERSION." >&2
  exit 1
fi
mark_phase_passed
CURRENT_PHASE="source-stage"

STAGING_DIR="$(mktemp -d "$OUTPUT_DIR/.build-$RELEASE_SLUG.XXXXXX")"
chmod 0755 "$STAGING_DIR"

# Stage exactly the tracked bytes from the captured commit. Ignored workspace
# files (including dotenv files, logs, caches, and local dependencies) can
# never enter the release through a blacklist gap.
git -C "$REPO_ROOT" archive --format=tar "$SOURCE_GIT_COMMIT" platform \
  | /usr/bin/tar --no-same-permissions -xf - \
    -C "$STAGING_DIR" --strip-components=1
if [[ -n "$(find "$STAGING_DIR" -type l -print -quit)" ]]; then
  echo "Release build refused: tracked source contains a symlink." >&2
  exit 1
fi
# The read-only outage diagnostics workflow executes these tools from the
# immutable current release.  Keep the dependency explicit so a future
# archive/pruning change cannot publish a release that silently loses its
# aggregate-only evidence path.
for runtime_helper in \
  "tools/platform_nginx_error_summary.py" \
  "tools/platform_web_runtime_diagnostics_summary.py" \
  "tools/platform_storage_evidence_summary.py" \
  "tools/platform_media_migration_diagnostics_summary.py"; do
  if [[ ! -s "$STAGING_DIR/$runtime_helper" || -L "$STAGING_DIR/$runtime_helper" ]]; then
    echo "Release build refused: tracked runtime diagnostic helper is missing: $runtime_helper" >&2
    exit 1
  fi
done
rm -rf \
  "$STAGING_DIR/.github" \
  "$STAGING_DIR/AGENTS.md" \
  "$STAGING_DIR/docs" \
  "$STAGING_DIR/tests" \
  "$STAGING_DIR/apps/platform_web/AGENTS.md"
mark_phase_passed
CURRENT_PHASE="web-dependencies"

NPM_CLI="$PINNED_NODE_HOME/lib/node_modules/npm/bin/npm-cli.js"
PACKAGE_MANAGER="$({ /usr/bin/python3 -I - "$STAGING_DIR/apps/platform_web/package.json" <<'PY'
import json
from pathlib import Path
import re
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
package_manager = payload.get("packageManager")
if not isinstance(package_manager, str) or re.fullmatch(r"npm@[0-9]+\.[0-9]+\.[0-9]+", package_manager) is None:
    raise SystemExit("Tracked package.json must pin an exact npm version.")
print(package_manager)
PY
} 2>&1)" || {
  echo "$PACKAGE_MANAGER" >&2
  exit 1
}
PINNED_NPM_VERSION="$(
  /usr/bin/env -i \
    HOME=/nonexistent \
    LANG=C.UTF-8 \
    PATH="$PINNED_NODE_HOME/bin:/usr/bin:/bin" \
    "$PLATFORM_NODE_BIN" "$NPM_CLI" --version
)"
if [[ "$PINNED_NPM_VERSION" != "$EXPECTED_NPM_VERSION" \
  || "$PACKAGE_MANAGER" != "npm@$PINNED_NPM_VERSION" ]]; then
  echo "Pinned Node runtime npm does not match tracked package.json: $PACKAGE_MANAGER." >&2
  exit 1
fi
NPM_CACHE="$STAGING_DIR/.npm-cache"
mkdir -m 0700 "$NPM_CACHE"
/usr/bin/env -i \
  HOME=/nonexistent \
  LANG=C.UTF-8 \
  PATH="$PINNED_NODE_HOME/bin:/usr/bin:/bin" \
  PLATFORM_NODE_BIN="$PLATFORM_NODE_BIN" \
  npm_config_cache="$NPM_CACHE" \
  "$PLATFORM_NODE_BIN" "$NPM_CLI" ci \
    --ignore-scripts --no-audit --no-fund \
    --prefix "$STAGING_DIR/apps/platform_web"
rm -rf "$NPM_CACHE"
mark_phase_passed
CURRENT_PHASE="live-qa-runtime"

# Build the credential-bearing browser runtime from the exact lock-installed
# Playwright packages before the full application dependency tree is removed.
# The builder copies only the pinned Node executable, Playwright packages,
# reviewed live-journey sources and checksum-pinned browser archives; it never
# publishes the application's arbitrary node_modules tree.
"$ROOT_DIR/.venv_platform/bin/python" -I \
  "$STAGING_DIR/tools/platform_build_live_qa_runtime.py" \
  --platform-root "$STAGING_DIR" \
  --node-home "$PINNED_NODE_HOME" \
  --output "$STAGING_DIR/liveqa-runtime"
if [[ ! -d "$STAGING_DIR/liveqa-runtime" || -L "$STAGING_DIR/liveqa-runtime" \
  || ! -f "$STAGING_DIR/liveqa-runtime/runtime-manifest.json" \
  || -L "$STAGING_DIR/liveqa-runtime/runtime-manifest.json" ]]; then
  echo "Release build refused: live-QA runtime artifact is incomplete." >&2
  exit 1
fi
rm -rf "$STAGING_DIR/apps/platform_web/tests"
mark_phase_passed
CURRENT_PHASE="python-wheelhouse"

WHEELHOUSE_DIR="$STAGING_DIR/wheelhouse"
mkdir -m 0700 "$WHEELHOUSE_DIR"
/usr/bin/env -i \
  HOME=/nonexistent \
  LANG=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  "$ROOT_DIR/.venv_platform/bin/python" -I -m pip download \
  --only-binary=:all: \
  --index-url https://pypi.org/simple \
  --dest "$WHEELHOUSE_DIR" \
  --require-hashes \
  --requirement "$STAGING_DIR/requirements-platform.lock.txt"
VERIFY_VENV="$STAGING_DIR/.wheelhouse-verify-venv"
/usr/bin/python3 -I -m venv "$VERIFY_VENV"
PINNED_PIP_VERSION="$(
  /usr/bin/python3 -I - "$STAGING_DIR/requirements-platform.lock.txt" <<'PY'
from pathlib import Path
import re
import sys

lock = Path(sys.argv[1])
versions = [
    line.split(" --hash=", 1)[0].removeprefix("pip==").strip()
    for line in lock.read_text(encoding="utf-8").splitlines()
    if line.startswith("pip==")
]
if len(versions) != 1:
    raise SystemExit("Tracked Python lock must contain exactly one pinned pip version.")
version = versions[0]
if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+!-]*", version) is None:
    raise SystemExit("Tracked pip version is unsafe.")
print(version)
PY
)"
PIP_WHEELS=("$WHEELHOUSE_DIR"/pip-"$PINNED_PIP_VERSION"-*.whl)
if (( ${#PIP_WHEELS[@]} != 1 )) || [[ ! -f "${PIP_WHEELS[0]}" ]]; then
  echo "Release wheelhouse must contain exactly one pinned pip wheel." >&2
  exit 1
fi
/usr/bin/env -i \
  HOME=/nonexistent \
  LANG=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  "$VERIFY_VENV/bin/python" -I -m pip install \
    --no-index --no-deps --force-reinstall "${PIP_WHEELS[0]}"
/usr/bin/env -i \
  HOME=/nonexistent \
  LANG=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  "$VERIFY_VENV/bin/python" -I -m pip install \
    --no-index \
    --find-links "$WHEELHOUSE_DIR" \
    --only-binary=:all: \
    --upgrade \
    --force-reinstall \
    --require-hashes \
    --requirement "$STAGING_DIR/requirements-platform.lock.txt"
/usr/bin/env -i \
  HOME=/nonexistent \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PIP_NO_INDEX=1 \
  "$VERIFY_VENV/bin/python" -I -m pip check
/usr/bin/env -i \
  HOME=/nonexistent \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PIP_NO_INDEX=1 \
  "$VERIFY_VENV/bin/python" -I -m pip freeze --all \
  | /usr/bin/sort >"$STAGING_DIR/requirements-platform.freeze.txt"
chmod 0444 "$STAGING_DIR/requirements-platform.freeze.txt"
rm -rf "$VERIFY_VENV"
if ! /usr/bin/python3 -I - \
  "$STAGING_DIR/requirements-platform.lock.txt" \
  "$STAGING_DIR/requirements-platform.freeze.txt" <<'PY'
from pathlib import Path
import re
import sys


def pins(path: Path) -> list[str]:
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        pin = line.split(" --hash=", 1)[0]
        if re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", pin) is None:
            raise SystemExit(f"invalid exact package pin in {path}")
        values.append(pin)
    return values


lock, freeze = (pins(Path(path)) for path in sys.argv[1:])
if lock != freeze:
    raise SystemExit(1)
PY
then
  echo "Resolved Python freeze does not match the tracked lock." >&2
  exit 1
fi
"$ROOT_DIR/.venv_platform/bin/python" -I \
  "$STAGING_DIR/tools/platform_validate_wheelhouse.py" create \
  --wheelhouse "$WHEELHOUSE_DIR" \
  --requirements "$STAGING_DIR/requirements-platform.txt" \
  --lock "$STAGING_DIR/requirements-platform.lock.txt" \
  --freeze "$STAGING_DIR/requirements-platform.freeze.txt"
"$ROOT_DIR/.venv_platform/bin/python" -I \
  "$STAGING_DIR/tools/platform_validate_wheelhouse.py" verify \
  --wheelhouse "$WHEELHOUSE_DIR" \
  --requirements "$STAGING_DIR/requirements-platform.txt" \
  --lock "$STAGING_DIR/requirements-platform.lock.txt" \
  --freeze "$STAGING_DIR/requirements-platform.freeze.txt"
mark_phase_passed
CURRENT_PHASE="dependency-baseline"

if [[ -n "$DEPENDENCY_BASELINE" ]]; then
  REVALIDATED_BASELINE="$(validate_dependency_baseline "$DEPENDENCY_BASELINE")"
  if [[ "$REVALIDATED_BASELINE" != "$DEPENDENCY_BASELINE" \
    || "$(path_identity "$DEPENDENCY_BASELINE")" != "$BASELINE_ID" ]]; then
    echo "Dependency baseline changed during the release build." >&2
    exit 1
  fi
  BASELINE_FILES=(
    requirements-platform.txt
    requirements-platform.lock.txt
    requirements-platform.freeze.txt
    wheelhouse/WHEELHOUSE.sha256
    apps/platform_web/package-lock.json
  )
  for BASELINE_FILE in "${BASELINE_FILES[@]}"; do
    if ! /usr/bin/cmp -s \
      "$DEPENDENCY_BASELINE/$BASELINE_FILE" \
      "$STAGING_DIR/$BASELINE_FILE"; then
      echo "Dependency baseline mismatch: $BASELINE_FILE" >&2
      exit 1
    fi
  done
fi
mark_phase_passed
CURRENT_PHASE="web-build"

(
  cd "$STAGING_DIR/apps/platform_web"
  /usr/bin/env -i \
    CI=1 \
    HOME=/nonexistent \
    LANG=C.UTF-8 \
    NEXT_TELEMETRY_DISABLED=1 \
    PATH="$PINNED_NODE_HOME/bin:/usr/bin:/bin" \
    PLATFORM_NODE_BIN="$PLATFORM_NODE_BIN" \
    PLATFORM_WEB_NEXT_COMPRESSION="$WEB_NEXT_COMPRESSION" \
    "$PLATFORM_NODE_BIN" "$NPM_CLI" run typecheck
  /usr/bin/env -i \
    CI=1 \
    HOME=/nonexistent \
    LANG=C.UTF-8 \
    NEXT_TELEMETRY_DISABLED=1 \
    PATH="$PINNED_NODE_HOME/bin:/usr/bin:/bin" \
    PLATFORM_NODE_BIN="$PLATFORM_NODE_BIN" \
    PLATFORM_WEB_NEXT_COMPRESSION="$WEB_NEXT_COMPRESSION" \
    "$PLATFORM_NODE_BIN" "$NPM_CLI" run build
  rm -rf .next/standalone/.next/static
  mkdir -p .next/standalone/.next
  cp -R .next/static .next/standalone/.next/static
  if [[ -d public ]]; then
    rm -rf .next/standalone/public
    cp -R public .next/standalone/public
  fi
  # Next.js standalone serves from this tree and may create its incremental
  # runtime cache there. Never carry a cache from the build into an immutable
  # release; the service preparer creates the empty service-owned directory.
  rm -rf .next/standalone/.next/cache
  rm -rf node_modules .next/cache
)

if [[ ! -f "$STAGING_DIR/apps/platform_web/.next/standalone/server.js" ]]; then
  echo "Release build is missing the Next.js standalone server artifact." >&2
  exit 1
fi

if [[ ! -d "$STAGING_DIR/apps/platform_web/.next/standalone/.next/static" ]]; then
  echo "Release build is missing the Next.js standalone static assets." >&2
  exit 1
fi

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all -- platform)" \
  || "$(git -C "$REPO_ROOT" rev-parse --verify HEAD)" != "$SOURCE_GIT_COMMIT" ]]; then
  echo "Release build refused: platform source changed during the build." >&2
  exit 1
fi
WEB_BUILD_ID="$(cat "$STAGING_DIR/apps/platform_web/.next/BUILD_ID")"
mark_phase_passed
CURRENT_PHASE="release-metadata"

/usr/bin/python3 -I - \
  "$STAGING_DIR/RELEASE.json" \
  "$RELEASE_SLUG" \
  "$BUILD_TIMESTAMP" \
  "$RELEASE_REF" \
  "$SOURCE_GIT_COMMIT" \
  "$WEB_BUILD_ID" \
  "$NODE_VERSION" \
  "$PINNED_NPM_VERSION" <<'PY'
import json
from pathlib import Path
import re
import sys

output, slug, built_at, release_ref, commit, web_build_id, node_version, npm_version = (
    sys.argv[1:]
)
if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", web_build_id) is None:
    raise SystemExit("Next.js emitted an unsafe build ID.")
payload = {
    "artifact_format_version": 1,
    "release_slug": slug,
    "built_at_utc": built_at,
    "release_ref": release_ref,
    "source_git_commit": commit,
    "python_requirements_file": "requirements-platform.txt",
    "python_lock_file": "requirements-platform.lock.txt",
    "python_freeze_file": "requirements-platform.freeze.txt",
    "python_wheelhouse_dir": "wheelhouse",
    "python_wheelhouse_manifest_file": "wheelhouse/WHEELHOUSE.sha256",
    "web_package_lock_file": "apps/platform_web/package-lock.json",
    "web_build_id": web_build_id,
    "node_version": node_version,
    "npm_version": npm_version,
}
Path(output).write_text(
    json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
    encoding="ascii",
)
PY
chmod 0444 "$STAGING_DIR/RELEASE.json"
mark_phase_passed
CURRENT_PHASE="artifact-promote"

# npm/Next may preserve group-writable modes from package archives even under
# the build umask. Preserve every executable/read-only bit while removing only
# group/world write authority before the immutable tree is promoted.
/usr/bin/chmod -R go-w -- "$STAGING_DIR"
if [[ -n "$(find "$STAGING_DIR" ! -type l -perm /022 -print -quit)" ]]; then
  echo "Release staging tree retains unsafe writable permissions." >&2
  exit 1
fi

RELEASE_ID="$(path_identity "$STAGING_DIR")"
trap '' HUP INT TERM
/usr/bin/mv -nT -- "$STAGING_DIR" "$RELEASE_DIR"
if [[ ! -d "$RELEASE_DIR" || -L "$RELEASE_DIR" \
  || "$(path_identity "$RELEASE_DIR")" != "$RELEASE_ID" ]]; then
  echo "Release directory promotion was not exclusive." >&2
  exit 1
fi
STAGING_DIR=""
trap - HUP INT TERM
trap 'interrupt_build 130' INT
trap 'interrupt_build 143' TERM
SOURCE_DATE_EPOCH="$(git -C "$REPO_ROOT" show -s --format=%ct "$SOURCE_GIT_COMMIT")"
ARTIFACT_TEMP="$(mktemp "$OUTPUT_DIR/.artifact-$RELEASE_SLUG.XXXXXX")"
(
  cd "$OUTPUT_DIR"
  /usr/bin/tar \
    --sort=name \
    --format=posix \
    --pax-option=delete=atime,delete=ctime \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    --mtime="@$SOURCE_DATE_EPOCH" \
    -cf - "$RELEASE_SLUG"
) | /usr/bin/gzip -n >"$ARTIFACT_TEMP"
chmod 0644 "$ARTIFACT_TEMP"
ARTIFACT_ID="$(path_identity "$ARTIFACT_TEMP")"
trap '' HUP INT TERM
/usr/bin/mv -nT -- "$ARTIFACT_TEMP" "$ARTIFACT_PATH"
if [[ ! -f "$ARTIFACT_PATH" || -L "$ARTIFACT_PATH" \
  || "$(path_identity "$ARTIFACT_PATH")" != "$ARTIFACT_ID" ]]; then
  echo "Release artifact promotion was not exclusive." >&2
  exit 1
fi
ARTIFACT_TEMP=""
trap - HUP INT TERM
trap 'interrupt_build 130' INT
trap 'interrupt_build 143' TERM
CHECKSUM_TEMP="$(mktemp "$OUTPUT_DIR/.checksum-$RELEASE_SLUG.XXXXXX")"
(
  cd "$OUTPUT_DIR"
  /usr/bin/sha256sum "$(basename "$ARTIFACT_PATH")" >"$CHECKSUM_TEMP"
)
chmod 0644 "$CHECKSUM_TEMP"
CHECKSUM_ID="$(path_identity "$CHECKSUM_TEMP")"
trap '' HUP INT TERM
/usr/bin/mv -nT -- "$CHECKSUM_TEMP" "$ARTIFACT_SHA_PATH"
if [[ ! -f "$ARTIFACT_SHA_PATH" || -L "$ARTIFACT_SHA_PATH" \
  || "$(path_identity "$ARTIFACT_SHA_PATH")" != "$CHECKSUM_ID" ]]; then
  echo "Release checksum promotion was not exclusive." >&2
  exit 1
fi
CHECKSUM_TEMP=""
trap - HUP INT TERM
trap 'interrupt_build 130' INT
trap 'interrupt_build 143' TERM
mark_phase_passed
CURRENT_PHASE="artifact-validate"
/usr/bin/python3 -I "$ROOT_DIR/tools/platform_validate_release_artifact.py" \
  --artifact "$ARTIFACT_PATH" \
  --checksum "$ARTIFACT_SHA_PATH" \
  --release-slug "$RELEASE_SLUG"

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all -- platform)" \
  || "$(git -C "$REPO_ROOT" rev-parse --verify HEAD)" != "$SOURCE_GIT_COMMIT" ]]; then
  echo "Release build refused: platform source changed before artifact completion." >&2
  exit 1
fi
ARTIFACT_SHA256="$(/usr/bin/sha256sum "$ARTIFACT_PATH" | awk '{print $1}')"
if [[ ! "$ARTIFACT_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Release build refused: artifact digest is invalid." >&2
  exit 1
fi
mark_phase_passed
BUILD_COMPLETE=1

# Build output is a public CI channel.  Keep the machine layout and command
# line private while retaining the validated release identity and source
# provenance needed by the caller.
printf 'RELEASE_BUILD schema=1 status=passed class=build release_slug=%s source_sha=%s artifact=validated\n' \
  "$RELEASE_SLUG" "$SOURCE_GIT_COMMIT"
