#!/usr/bin/env bash
set -euo pipefail

# Install the one immutable Python environment used by non-editable CI jobs.
# The lock is intentionally scoped to the Python 3.12 x86_64 Ubuntu runner
# used by platform-security.yml: several native wheels are platform-specific.
# Unsupported interpreters/architectures fail closed instead of resolving a
# different dependency set.

PLATFORM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${PLATFORM_CI_LOCK_FILE:-$PLATFORM_ROOT/requirements-ci.lock.txt}"
VENV_DIR="${PLATFORM_CI_VENV_DIR:-$PLATFORM_ROOT/.venv_platform}"
PYTHON_BIN="${PLATFORM_CI_PYTHON:-python3}"
CLEAN_ENV_TOOL="$PLATFORM_ROOT/tools/platform_ci_pip_env.sh"

fail() {
  echo "CI Python dependency install blocked: $*" >&2
  exit 1
}

if [[ "$LOCK_FILE" != /* ]]; then
  LOCK_FILE="$PLATFORM_ROOT/$LOCK_FILE"
fi
[[ -f "$LOCK_FILE" && ! -L "$LOCK_FILE" ]] || fail "lock file is missing or a symlink: $LOCK_FILE"
[[ -x "$CLEAN_ENV_TOOL" && ! -L "$CLEAN_ENV_TOOL" ]] || fail \
  "pip environment wrapper is missing or not executable"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python interpreter is unavailable: $PYTHON_BIN"

python_contract="$($PYTHON_BIN -c 'import platform, sys; print(f"{sys.version_info[0]}.{sys.version_info[1]} {sys.platform} {platform.machine()}")')"
[[ "$python_contract" == "3.12 linux x86_64" ]] || fail \
  "unsupported runner contract ($python_contract); expected Python 3.12 linux x86_64"

if [[ -e "$VENV_DIR" || -L "$VENV_DIR" ]]; then
  fail "refusing to reuse an existing CI virtualenv: $VENV_DIR"
fi

"$PYTHON_BIN" -m venv "$VENV_DIR"
VENV_PYTHON="$VENV_DIR/bin/python"
[[ -x "$VENV_PYTHON" ]] || fail "venv creation did not produce $VENV_PYTHON"

# The venv's bundled pip is only a bootstrap interpreter. pip itself,
# setuptools and wheel are exact, hashed entries in requirements-ci.lock.txt;
# this one install upgrades the bootstrap set and all runtime/quality tools.
PIP_CONFIG_FILE=/dev/null "$CLEAN_ENV_TOOL" "$VENV_PYTHON" -m pip install \
  --isolated \
  --disable-pip-version-check \
  --no-input \
  --no-deps \
  --only-binary=:all: \
  --index-url https://pypi.org/simple \
  --require-hashes \
  --upgrade \
  --requirement "$LOCK_FILE"

PIP_CONFIG_FILE=/dev/null "$CLEAN_ENV_TOOL" "$VENV_PYTHON" -m pip check
