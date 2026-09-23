#!/usr/bin/env bash
set -euo pipefail

# Generate the CI lock from a hash-locked, ephemeral pip-tools toolchain.
#
# The default mode is a freshness check: existing package versions are passed
# as constraints and any byte change fails closed.  ``--update`` is the only
# mode allowed to resolve newer transitive versions; its diff must be reviewed
# and audited before it is accepted.

export LC_ALL=C

PLATFORM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT_FILE="$PLATFORM_ROOT/requirements-ci.in"
OUTPUT_FILE="$PLATFORM_ROOT/requirements-ci.lock.txt"
METADATA_FILE="$PLATFORM_ROOT/requirements-ci.lock.meta.json"
LOCKER_FILE="$PLATFORM_ROOT/requirements-ci-locker.lock.txt"
PYTHON_BIN="${PLATFORM_CI_PYTHON:-python3}"
EXPECTED_PIP_TOOLS_VERSION="7.6.1"
CANONICAL_PYPI_INDEX="https://pypi.org/simple"
CLEAN_ENV_TOOL="$PLATFORM_ROOT/tools/platform_ci_pip_env.sh"
MODE="${1:-}"

fail() {
  echo "CI dependency lock generation blocked: $*" >&2
  exit 1
}

case "$MODE" in
  "") UPDATE_MODE=false ;;
  --update) UPDATE_MODE=true ;;
  *) fail "unsupported argument $MODE (use --update only for a reviewed refresh)" ;;
esac

[[ -f "$INPUT_FILE" && ! -L "$INPUT_FILE" ]] || fail "input file is missing or a symlink"
[[ -f "$LOCKER_FILE" && ! -L "$LOCKER_FILE" ]] || fail "toolchain lock is missing or a symlink"
[[ -x "$CLEAN_ENV_TOOL" && ! -L "$CLEAN_ENV_TOOL" ]] || fail \
  "pip environment wrapper is missing or not executable"
if [[ "$UPDATE_MODE" == false ]]; then
  [[ -f "$OUTPUT_FILE" && ! -L "$OUTPUT_FILE" ]] || fail \
    "default freeze mode requires an existing CI lock; use --update for first generation"
  [[ -f "$METADATA_FILE" && ! -L "$METADATA_FILE" ]] || fail \
    "default freeze mode requires lock metadata; use --update to establish it"
else
  for output_path in "$OUTPUT_FILE" "$METADATA_FILE"; do
    if [[ -e "$output_path" || -L "$output_path" ]] && \
      [[ ! -f "$output_path" || -L "$output_path" ]]; then
      fail "update output must be a regular file, not a symlink or non-regular path: $output_path"
    fi
  done
fi
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python interpreter is unavailable: $PYTHON_BIN"

python_contract="$($PYTHON_BIN -c 'import platform, sys; print(f"{sys.version_info[0]}.{sys.version_info[1]} {sys.platform} {platform.machine()}")')"
[[ "$python_contract" == "3.12 linux x86_64" ]] || fail \
  "unsupported generation contract ($python_contract); expected Python 3.12 linux x86_64"

validate_lock() {
  local candidate="$1"
  [[ -f "$candidate" && ! -L "$candidate" ]] || fail "lock is missing or a symlink: $candidate"
  [[ -s "$candidate" ]] || fail "lock is empty: $candidate"
  if grep -Eqv \
    '^[A-Za-z0-9_.+-]+==[A-Za-z0-9][A-Za-z0-9_.+!-]* --hash=sha256:[0-9a-f]{64}$' \
    "$candidate"; then
    fail "lock contains an unhashed or non-exact requirement: $candidate"
  fi
  sort -c "$candidate" >/dev/null || fail "lock is not canonically sorted: $candidate"
  if awk '
    {
      split($1, fields, "==")
      name = tolower(fields[1])
      gsub(/[-_.]/, "-", name)
      if (seen[name]++) bad = 1
    }
    END { exit bad ? 1 : 0 }
  ' "$candidate"; then
    :
  else
    fail "lock contains duplicate normalized package names: $candidate"
  fi
}

validate_lock "$LOCKER_FILE"
grep -Eq '^pip-tools==7\.6\.1 --hash=sha256:[0-9a-f]{64}$' "$LOCKER_FILE" \
  || fail "toolchain lock does not pin pip-tools 7.6.1"
grep -Eq '^pip==[0-9][^ ]* --hash=sha256:[0-9a-f]{64}$' "$LOCKER_FILE" \
  || fail "toolchain lock does not pin pip"

TEMP_DIR="$(mktemp -d /tmp/platform-ci-lock.XXXXXX)"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
mkdir -m 0700 "$TEMP_DIR/wheelhouse"

"$PYTHON_BIN" -m venv "$TEMP_DIR/tool-venv"
TOOL_PYTHON="$TEMP_DIR/tool-venv/bin/python"
PIP_COMPILE_BIN="$TEMP_DIR/tool-venv/bin/pip-compile"
[[ -x "$TOOL_PYTHON" ]] || fail "tool venv creation did not produce Python"

"$CLEAN_ENV_TOOL" "$TOOL_PYTHON" -m pip install \
  --isolated \
  --disable-pip-version-check \
  --no-cache-dir \
  --no-input \
  --no-deps \
  --only-binary=:all: \
  --index-url "$CANONICAL_PYPI_INDEX" \
  --require-hashes \
  --requirement "$LOCKER_FILE"

tool_version="$("$CLEAN_ENV_TOOL" "$PIP_COMPILE_BIN" --version)"
[[ "$tool_version" == "pip-compile, version $EXPECTED_PIP_TOOLS_VERSION" ]] || fail \
  "bootstrapped pip-tools version is not $EXPECTED_PIP_TOOLS_VERSION: $tool_version"

# Download direct inputs without hash mode first.  The existing lock is
# downloaded separately with --require-hashes because mixing unhashed direct
# inputs and a hash-bearing requirements file makes pip reject the command.
"$CLEAN_ENV_TOOL" "$TOOL_PYTHON" -m pip download \
  --isolated \
  --disable-pip-version-check \
  --no-cache-dir \
  --only-binary=:all: \
  --index-url "$CANONICAL_PYPI_INDEX" \
  --dest "$TEMP_DIR/wheelhouse" \
  --requirement "$INPUT_FILE"
if [[ "$UPDATE_MODE" == false ]]; then
  # Download the existing artifacts as well as direct inputs.  This prevents a
  # newer PyPI candidate from making default freeze mode drift merely because
  # the old wheel is no longer the resolver's first candidate.
  "$CLEAN_ENV_TOOL" "$TOOL_PYTHON" -m pip download \
    --isolated \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-deps \
    --only-binary=:all: \
    --index-url "$CANONICAL_PYPI_INDEX" \
    --dest "$TEMP_DIR/wheelhouse" \
    --require-hashes \
    --requirement "$OUTPUT_FILE"
fi

compile_args=(
  --no-index
  --find-links "$TEMP_DIR/wheelhouse"
  --generate-hashes
  --allow-unsafe
  --strip-extras
  --no-header
  --no-annotate
  --no-emit-index-url
  --no-emit-find-links
  --output-file "$TEMP_DIR/compiled.txt"
)
if [[ "$UPDATE_MODE" == false ]]; then
  compile_args+=(--constraint "$OUTPUT_FILE")
fi
"$CLEAN_ENV_TOOL" "$PIP_COMPILE_BIN" "${compile_args[@]}" "$INPUT_FILE"

# pip-tools emits continuation lines and an unsafe-package comment. Keep the
# tracked lock intentionally simple: one exact package pin and one generated
# SHA-256 hash per line.
awk '
  /^[[:alnum:]_.+-]+==/ {
    package = $0
    sub(/[[:space:]]+\\$/, "", package)
    next
  }
  /^[[:space:]]+--hash=sha256:[0-9a-f]{64}$/ {
    hash = $0
    sub(/^[[:space:]]+/, "", hash)
    if (package == "") exit 2
    print package " " hash
    package = ""
    next
  }
  /^$/ || /^#/ { next }
  { exit 3 }
  END { if (package != "") exit 4 }
' "$TEMP_DIR/compiled.txt" | sort > "$TEMP_DIR/lock.txt" \
  || fail "pip-compile produced an unparseable lock"
validate_lock "$TEMP_DIR/lock.txt"

if [[ "$UPDATE_MODE" == false ]]; then
  cmp -s "$TEMP_DIR/lock.txt" "$OUTPUT_FILE" || fail \
    "default freeze would change the lock; review the diff and rerun with --update"
fi

# Metadata includes the complete -r input closure, not only requirements-ci.in,
# so a newly added include cannot evade freshness or ownership checks.
"$CLEAN_ENV_TOOL" "$TOOL_PYTHON" - "$PLATFORM_ROOT" "$INPUT_FILE" "$LOCKER_FILE" \
  "$TEMP_DIR/lock.txt" "$TEMP_DIR/metadata.json" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys

platform_root = Path(sys.argv[1]).resolve()
input_file = Path(sys.argv[2]).resolve()
toolchain_lock = Path(sys.argv[3]).resolve()
generated_lock = Path(sys.argv[4]).resolve()
metadata_file = Path(sys.argv[5]).resolve()
include_re = re.compile(r"^(?:-r|--requirement)\s+(?P<path>[^\s#]+)")
seen: set[Path] = set()
ordered: list[Path] = []


def walk(path: Path) -> None:
    path = path.resolve()
    if path in seen:
        return
    if platform_root not in path.parents:
        raise SystemExit(f"requirement include escapes platform root: {path}")
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"requirement include is missing or a symlink: {path}")
    seen.add(path)
    ordered.append(path)
    for line in path.read_text(encoding="utf-8").splitlines():
        match = include_re.match(line.strip())
        if match:
            walk(path.parent / match.group("path"))


walk(input_file)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


relative_inputs = [
    {
        "path": str(path.relative_to(platform_root)),
        "sha256": digest(path),
    }
    for path in sorted(ordered)
]
payload = {
    "schema": 1,
    "target": {
        "python": "3.12",
        "platform": "linux",
        "architecture": "x86_64",
    },
    "index_url": "https://pypi.org/simple",
    "input_files": relative_inputs,
    "lock_file": "requirements-ci.lock.txt",
    "lock_sha256": digest(generated_lock),
    "toolchain_lock_file": "requirements-ci-locker.lock.txt",
    "toolchain_lock_sha256": digest(toolchain_lock),
    "toolchain": {
        "pip-tools": "7.6.1",
        "pip": "26.2.1",
        "setuptools": "84.0.0",
        "wheel": "0.48.0",
    },
    "freshness_policy": {
        "default": "reuse-existing-locked-versions",
        "update": "explicit --update required for newer resolution",
    },
}
metadata_file.write_text(
    json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
PY

if [[ "$UPDATE_MODE" == false ]]; then
  cmp -s "$TEMP_DIR/metadata.json" "$METADATA_FILE" || fail \
    "default freeze metadata is stale; review the diff and rerun with --update"
else
  install -m 0644 "$TEMP_DIR/lock.txt" "$OUTPUT_FILE"
  install -m 0644 "$TEMP_DIR/metadata.json" "$METADATA_FILE"
fi

sha256sum "$OUTPUT_FILE" "$METADATA_FILE" "$LOCKER_FILE"
