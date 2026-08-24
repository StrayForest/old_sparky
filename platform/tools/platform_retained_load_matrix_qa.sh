#!/usr/bin/env bash
set +x
set -euo pipefail

TRUSTED_REPO_ROOT="/root/old_sparky"
PLATFORM_ROOT="$TRUSTED_REPO_ROOT/platform"
TOOLS_DIR="$PLATFORM_ROOT/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_retained_load_matrix_qa.sh"
QA_PYTHON="$PLATFORM_ROOT/.venv_platform/bin/python"
RUNTIME_ROOT="/opt/oldsparky/platform"
OUTPUT_ROOT_BASE="$RUNTIME_ROOT/shared/preprod-retained-matrix"
SYSTEM_PYTHON="/usr/bin/python3.12"
CONFIRMATION="RUN-RETAINED-LOAD-MATRIX"
LOCK_PATH="/run/lock/oldsparky-retained-load-matrix.lock"

if [[ "$EUID" -ne 0 ]]; then
  echo "Retained load matrix supervisor must run as root." >&2
  exit 1
fi
if [[ "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")" != "$SCRIPT_PATH" ]]; then
  echo "Retained load matrix must run from the fixed root-controlled checkout." >&2
  exit 1
fi
exec 9>"$LOCK_PATH"
flock -n 9 || {
  echo "Another retained load matrix is already running on this host." >&2
  exit 1
}
if (( $# != 5 )) || [[ "$1" != "$CONFIRMATION" ]]; then
  echo "Usage: $0 $CONFIRMATION <target-sha> <control-email> <concurrency> <run-id>" >&2
  exit 2
fi

confirmation="$1"
target_sha="$2"
control_email="$3"
concurrency="$4"
run_id="$5"

test "$confirmation" = "$CONFIRMATION"
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
  echo "Trusted pre-production checkout is missing." >&2
  exit 1
}
test -x "$QA_PYTHON" || {
  echo "Pre-production QA Python runtime is missing." >&2
  exit 1
}
test -L "$RUNTIME_ROOT/current" || {
  echo "Active pre-production release is missing." >&2
  exit 1
}

checkout_sha="$(git -C "$TRUSTED_REPO_ROOT" rev-parse --verify HEAD)"
test "$checkout_sha" = "$target_sha" || {
  echo "Trusted pre-production checkout does not match the tested SHA." >&2
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
  echo "Active pre-production release does not match the tested SHA." >&2
  exit 1
}

platform_environment="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_ENVIRONMENT)"
case "$platform_environment" in
  preprod|staging|test|development) ;;
  *)
    echo "Refusing retained load matrix outside a non-production environment." >&2
    exit 1
    ;;
esac
platform_origin="$($SYSTEM_PYTHON -I "$TOOLS_DIR/platform_safe_env_exec.py" print-public-value PLATFORM_WEB_ORIGIN)"
case "$platform_origin" in
  https://old-sparky.com|https://www.old-sparky.com)
    echo "Refusing retained load matrix against the canonical production origin." >&2
    exit 1
    ;;
esac

run_root="$OUTPUT_ROOT_BASE/gha-$run_id"
export_dir="/tmp/old-sparky-retained-load-$run_id"
export_uid="${SUDO_UID:-0}"
export_gid="${SUDO_GID:-0}"
[[ "$export_uid" =~ ^[0-9]+$ && "$export_gid" =~ ^[0-9]+$ ]] || {
  echo "Unable to determine the SSH caller identity for report export." >&2
  exit 1
}

install -d -o root -g root -m 0700 "$run_root"
rm -rf -- "$export_dir"
install -d -o "$export_uid" -g "$export_gid" -m 0700 "$export_dir"
log_path="$run_root/matrix.log"

set +e
"$QA_PYTHON" "$TOOLS_DIR/platform_seed_retained_tournament_matrix.py" \
  --origin http://127.0.0.1 \
  --control-email "$control_email" \
  --concurrency "$concurrency" \
  --output-root "$run_root" \
  2>&1 | tee "$log_path"
pipeline_status=("${PIPESTATUS[@]}")
qa_status="${pipeline_status[0]}"
tee_status="${pipeline_status[1]}"
set -e
if [[ "$tee_status" != "0" ]]; then
  qa_status=1
fi

summary_path="$(find "$run_root" -mindepth 2 -maxdepth 2 -type f -name matrix-summary.json -print -quit)"
if [[ -z "$summary_path" ]]; then
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
            "error": "retained_load_matrix_summary_missing",
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
fi

install -o "$export_uid" -g "$export_gid" -m 0600 "$summary_path" "$export_dir/matrix-summary.json"
install -o "$export_uid" -g "$export_gid" -m 0600 "$log_path" "$export_dir/matrix.log"
printf 'RETAINED_LOAD_MATRIX_EXPORT=%s\n' "$export_dir"
printf 'RETAINED_LOAD_MATRIX_SUMMARY=%s\n' "$export_dir/matrix-summary.json"
printf 'RETAINED_LOAD_MATRIX_EXIT_CODE=%s\n' "$qa_status"
exit "$qa_status"
