#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REQUIRE_PREVIOUS=0
ALLOW_NO_PREVIOUS=0
ALLOW_INITIAL_INSTALL=0
REQUIRE_VERIFIED_BACKUP=0
REQUIRE_EDGE_PARITY=0
BACKUP_MAX_AGE_HOURS="24"
EXPECTED_NODE_VERSION="26.3.1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      if [[ $# -lt 2 ]]; then
        echo "--app-dir requires a path." >&2
        exit 1
      fi
      APP_DIR="$2"
      shift 2
      ;;
    --require-previous)
      REQUIRE_PREVIOUS=1
      shift
      ;;
    --allow-no-previous)
      ALLOW_NO_PREVIOUS=1
      shift
      ;;
    --allow-initial-install)
      ALLOW_INITIAL_INSTALL=1
      ALLOW_NO_PREVIOUS=1
      shift
      ;;
    --require-verified-backup)
      REQUIRE_VERIFIED_BACKUP=1
      shift
      ;;
    --require-edge-parity)
      REQUIRE_EDGE_PARITY=1
      shift
      ;;
    --backup-max-age-hours)
      if [[ $# -lt 2 ]]; then
        echo "--backup-max-age-hours requires a number." >&2
        exit 1
      fi
      BACKUP_MAX_AGE_HOURS="$2"
      shift 2
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_release_preflight.sh [--app-dir <path>] [--require-previous]
       [--allow-no-previous] [--allow-initial-install]
       [--require-verified-backup] [--require-edge-parity]
       [--backup-max-age-hours <hours>]

Validates the live platform release layout before or after a deploy.
EOF
      exit 0
      ;;
    *)
      echo "RELEASE_PREFLIGHT status=failed class=argument" >&2
      exit 1
      ;;
  esac
done

if [[ "$REQUIRE_PREVIOUS" -eq 1 && "$ALLOW_NO_PREVIOUS" -eq 1 ]]; then
  echo "--require-previous and --allow-no-previous are mutually exclusive." >&2
  exit 1
fi

CURRENT_TARGET=""
PREVIOUS_TARGET=""
if [[ -L "$APP_DIR/current" ]]; then
  CURRENT_TARGET="$(readlink -f "$APP_DIR/current" 2>/dev/null || true)"
elif [[ -e "$APP_DIR/current" ]]; then
  fail "Current release pointer is unsafe."
fi
if [[ -L "$APP_DIR/previous" ]]; then
  PREVIOUS_TARGET="$(readlink -f "$APP_DIR/previous" 2>/dev/null || true)"
elif [[ -e "$APP_DIR/previous" ]]; then
  fail "Previous release pointer is unsafe."
fi
SHARED_DIR="$APP_DIR/shared"
ENV_FILE="$SHARED_DIR/.env.platform"
PYTHON_BIN="$SHARED_DIR/venv/bin/python"
NODE_BIN="${PLATFORM_NODE_BIN:-$SHARED_DIR/node-v26.3.1/bin/node}"
PUBLIC_RELEASE_SLUG="unavailable"
PUBLIC_SOURCE_SHA="unavailable"
PUBLIC_RESULT_STATUS="passed"

fail() {
  exit 1
}

if [[ -n "$CURRENT_TARGET" && -d "$CURRENT_TARGET" ]]; then
  candidate_slug="$(basename "$CURRENT_TARGET")"
  if [[ "$candidate_slug" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
    PUBLIC_RELEASE_SLUG="$candidate_slug"
    candidate_sha="$({
      /usr/bin/python3 -I -B - "$CURRENT_TARGET/RELEASE.json" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="ascii")).get(
        "source_git_commit", ""
    )
except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
    value = ""
if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40,64}", value):
    print(value)
PY
    } 2>/dev/null)"
    [[ -n "$candidate_sha" ]] && PUBLIC_SOURCE_SHA="$candidate_sha"
  fi
fi

public_status() {
  local status="$1"
  printf 'RELEASE_PREFLIGHT schema=1 status=%s class=preflight release_slug=%s source_sha=%s\n' \
    "$status" "$PUBLIC_RELEASE_SLUG" "$PUBLIC_SOURCE_SHA"
}

on_exit() {
  local exit_status="$?"
  trap - EXIT
  if [[ "$exit_status" -ne 0 ]]; then
    public_status failed >&2
  fi
  exit "$exit_status"
}
trap on_exit EXIT

pass() {
  return 0
}

load_env_as_data() {
  local safe_env_tool="$SCRIPT_DIR/platform_safe_env_exec.py"
  [[ -f "$safe_env_tool" && ! -L "$safe_env_tool" ]] \
    || fail "Safe environment parser is missing or unsafe."
  local encoded_assignments
  encoded_assignments="$(
    /usr/bin/python3 -I -B "$safe_env_tool" export-b64 --path "$ENV_FILE" 2>/dev/null
  )" || fail "Canonical environment could not be parsed safely."
  local key encoded value
  while IFS=$'\t' read -r key encoded; do
    [[ -n "$key" ]] || continue
    value="$(printf '%s' "$encoded" | /usr/bin/base64 --decode 2>/dev/null)" \
      || fail "Canonical environment value could not be decoded: $key"
    export "$key=$value"
  done <<<"$encoded_assignments"
}

if [[ -z "$CURRENT_TARGET" || ! -d "$CURRENT_TARGET" ]]; then
  if [[ "$ALLOW_INITIAL_INSTALL" -ne 1 ]]; then
    fail "Current release is missing."
  fi
  [[ -z "$PREVIOUS_TARGET" ]] || fail "Initial install cannot have a previous release."
  [[ ! -e "$APP_DIR/current" && ! -L "$APP_DIR/current" ]] \
    || fail "Initial install current pointer is not absent."
  [[ ! -e "$APP_DIR/previous" && ! -L "$APP_DIR/previous" ]] \
    || fail "Initial install previous pointer is not absent."
  [[ ! -e "$SHARED_DIR/.release-systemd-state.json" \
    && ! -L "$SHARED_DIR/.release-systemd-state.json" ]] \
    || fail "Initial install has a systemd receipt."
  initial_transaction="$SHARED_DIR/.release-operation.json"
  initial_pending=0
  if [[ -e "$initial_transaction" || -L "$initial_transaction" ]]; then
    [[ -f "$initial_transaction" && ! -L "$initial_transaction" ]] \
      || fail "Initial install transaction metadata is unsafe."
    [[ "$(stat -c '%u:%g:%a:%h' "$initial_transaction" 2>/dev/null)" == "0:0:600:1" ]] \
      || fail "Initial install transaction metadata is unsafe."
    initial_context="$({
      /usr/bin/python3 -I - "$initial_transaction" <<'PY'
import json
import sys
from pathlib import Path

try:
    record = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)
if (
    not isinstance(record, dict)
    or record.get("operation") != "install"
    or record.get("current_before") is not None
    or record.get("previous_before") is not None
    or record.get("phase") not in {
        "quiesce-pending", "staged", "migration-pending", "migration-failed",
        "migration-applied", "activation-pending", "services-restarted",
        "nginx-pending", "nginx-applied", "smoke-passed", "activation-committed",
    }
):
    raise SystemExit(1)
print(record["phase"])
PY
    } 2>/dev/null)" || fail "Initial install transaction topology is invalid."
    initial_pending=1
    [[ "$initial_context" != "quiesce-pending" || ! -e "$SHARED_DIR/venv" ]] \
      || fail "Initial install quiesce receipt has an unexpected runtime."
  fi
  if [[ "$initial_pending" -eq 1 ]]; then
    [[ -d "$SHARED_DIR/venv" && ! -L "$SHARED_DIR/venv" ]] \
      || fail "Staged initial install runtime is missing."
    [[ "$(stat -c '%u:%g:%a:%h' "$SHARED_DIR/venv" 2>/dev/null)" =~ ^0:0:[0-7]+:[2-9][0-9]*$ ]] \
      || fail "Staged initial install runtime metadata is unsafe."
    [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] \
      || fail "Staged initial install env is missing."
    [[ "$(stat -c '%u:%g:%a:%h' "$ENV_FILE" 2>/dev/null)" == "0:0:600:1" ]] \
      || fail "Staged initial install env metadata is unsafe."
  else
    [[ ! -e "$SHARED_DIR/venv" && ! -L "$SHARED_DIR/venv" ]] \
      || fail "Initial install has an existing shared runtime."
  fi
  PUBLIC_RESULT_STATUS="review"
  public_status "$PUBLIC_RESULT_STATUS"
  exit 0
fi
pass

if [[ "$ALLOW_NO_PREVIOUS" -eq 1 ]]; then
  [[ -n "$CURRENT_TARGET" && -d "$CURRENT_TARGET" ]] \
    || fail "--allow-no-previous requires an existing current release."
  [[ ! -e "$APP_DIR/previous" && ! -L "$APP_DIR/previous" ]] \
    || fail "--allow-no-previous requires an absent previous release pointer."
  [[ -z "$PREVIOUS_TARGET" ]] \
    || fail "--allow-no-previous requires no previous release target."
  [[ ! -e "$SHARED_DIR/.release-systemd-state.json" \
    && ! -L "$SHARED_DIR/.release-systemd-state.json" ]] \
    || fail "--allow-no-previous requires no systemd receipt."
fi

if [[ "$REQUIRE_PREVIOUS" -eq 1 ]]; then
  [[ -n "$PREVIOUS_TARGET" && -d "$PREVIOUS_TARGET" ]] || fail "Previous release is missing."
fi
if [[ -n "$PREVIOUS_TARGET" && -d "$PREVIOUS_TARGET" ]]; then
  pass
else
  PUBLIC_RESULT_STATUS="review"
fi

[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "Shared env file is missing or unsafe: $ENV_FILE"
[[ "$(stat -c '%u:%g:%a:%h' "$ENV_FILE" 2>/dev/null)" == "0:0:600:1" ]] \
  || fail "Canonical env must remain root:root 0600 with one link."
pass

[[ -x "$PYTHON_BIN" ]] || fail "Shared Python runtime is missing: $PYTHON_BIN"
pass

[[ -x "$NODE_BIN" ]] || fail "Pinned shared Node runtime is missing: $NODE_BIN"
NODE_VERSION="$("$NODE_BIN" -p "process.versions.node" 2>/dev/null)"
[[ "$NODE_VERSION" == "$EXPECTED_NODE_VERSION" ]] \
  || fail "Node runtime must be exactly $EXPECTED_NODE_VERSION; got $NODE_VERSION."
pass

for required_path in \
  "$CURRENT_TARGET/RELEASE.json" \
  "$CURRENT_TARGET/apps/platform_web/.next/standalone/server.js" \
  "$CURRENT_TARGET/tools/platform_run_api.sh" \
  "$CURRENT_TARGET/tools/platform_run_worker.sh" \
  "$CURRENT_TARGET/tools/platform_run_web.sh" \
  "$CURRENT_TARGET/tools/platform_run_alembic.sh" \
  "$CURRENT_TARGET/tools/platform_safe_env_exec.py" \
  "$CURRENT_TARGET/tools/platform_deploy_smoke.py"; do
  [[ -e "$required_path" ]] || fail "Required release file is missing: $required_path"
done
pass

load_env_as_data
pass

[[ -d "$SHARED_DIR/env" ]] || fail "Rendered service env directory is missing: $SHARED_DIR/env"
RENDER_SERVICE_ENVS_TOOL="$SCRIPT_DIR/platform_render_service_envs.py"
[[ -f "$RENDER_SERVICE_ENVS_TOOL" && ! -L "$RENDER_SERVICE_ENVS_TOOL" ]] \
  || fail "Service env renderer is missing or unsafe."
"$PYTHON_BIN" -B "$RENDER_SERVICE_ENVS_TOOL" \
  --source "$ENV_FILE" \
  --output-dir "$SHARED_DIR/env" \
  --verify >/dev/null 2>/dev/null \
  || fail "Rendered service envs are stale or unsafe."
pass

SAFE_ENV_TOOL="$SCRIPT_DIR/platform_safe_env_exec.py"
[[ -f "$SAFE_ENV_TOOL" && ! -L "$SAFE_ENV_TOOL" ]] \
  || fail "Safe environment parser is missing or unsafe."
"$PYTHON_BIN" -I -B - \
  "$ENV_FILE" "$SAFE_ENV_TOOL" "$CURRENT_TARGET/tools/platform_deploy_smoke.py" \
  2>/dev/null <<'PY' \
  || fail "Deploy smoke dotenv interpretation diverges from the strict runtime parser."
import importlib.util
from pathlib import Path
import sys


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


env_path = Path(sys.argv[1])
safe = load_module("platform_safe_env_exec_preflight", Path(sys.argv[2]))
smoke = load_module("platform_deploy_smoke_preflight", Path(sys.argv[3]))
if safe.load_env_file(env_path) != smoke.load_env(env_path):
    raise SystemExit("strict and smoke dotenv parsers disagree")
PY
pass

if [[ "$REQUIRE_EDGE_PARITY" -eq 1 ]]; then
  EDGE_POLICY_TOOL="$SCRIPT_DIR/platform_validate_edge_policy.py"
  [[ -f "$EDGE_POLICY_TOOL" && ! -L "$EDGE_POLICY_TOOL" ]] \
    || fail "Edge policy validator is missing or unsafe."
  "$PYTHON_BIN" -B "$EDGE_POLICY_TOOL" \
    --json >/dev/null \
    || fail "Cloudflare/Nginx/UFW trust-range parity check failed."
  pass
fi

for required_env_key in \
  PLATFORM_ENVIRONMENT \
  PLATFORM_DATABASE_URL \
  PLATFORM_WEB_ORIGIN \
  PLATFORM_SECRET_KEY \
  PLATFORM_REDIS_URL \
  PLATFORM_CELERY_BROKER_URL \
  PLATFORM_CELERY_RESULT_BACKEND \
  PLATFORM_OBJECT_STORAGE_BACKEND \
  PLATFORM_R2_ENDPOINT_URL \
  PLATFORM_R2_ACCESS_KEY_ID \
  PLATFORM_R2_SECRET_ACCESS_KEY \
  PLATFORM_R2_BUCKET_NAME \
  PLATFORM_MEDIA_PUBLIC_BASE_URL; do
  if [[ -z "${!required_env_key:-}" ]]; then
    fail "Required env key is missing: $required_env_key"
  fi
done
pass "Required production env keys present"

CONFIG_CHECK_OUTPUT="$(
  cd "$CURRENT_TARGET" && \
  PLATFORM_RUNTIME_SERVICE=api \
  PLATFORM_ENV_FILE="$ENV_FILE" \
  PLATFORM_PYTHON_BIN="$PYTHON_BIN" \
  PYTHONPATH="$CURRENT_TARGET" \
  "$PYTHON_BIN" -B -c \
    "from python_packages.platform_infra.config import get_settings, validate_platform_settings; validate_platform_settings(get_settings(), require_api_secret=True); print('platform-config-ok')" \
    2>/dev/null
)"
[[ "$CONFIG_CHECK_OUTPUT" == *"platform-config-ok"* ]] \
  || fail "Production configuration contract validation failed."
pass

DB_CHECK_OUTPUT="$(
  cd "$CURRENT_TARGET" && \
  PLATFORM_ENV_FILE="$ENV_FILE" \
  PLATFORM_PYTHON_BIN="$PYTHON_BIN" \
  PYTHONPATH="$CURRENT_TARGET" \
  "$PYTHON_BIN" -B -c "import asyncio; from python_packages.platform_infra.db import warm_up_engine; asyncio.run(warm_up_engine()); print('platform-db-ok')" \
  2>/dev/null
)"
[[ "$DB_CHECK_OUTPUT" == *"platform-db-ok"* ]] || fail "Database warm-up check failed."
pass

# Do not route read-only Alembic introspection through the active release's
# shell wrapper: during the transition deployment that wrapper may predate the
# safe dotenv parser. The canonical env is already parsed and exported above.
ALEMBIC_CURRENT="$(
  cd "$CURRENT_TARGET" && \
  PYTHONPATH="$CURRENT_TARGET" \
  "$PYTHON_BIN" -B -m alembic current 2>/dev/null | tail -n 1 | awk '{print $1}'
)"
ALEMBIC_HEAD="$(
  cd "$CURRENT_TARGET" && \
  PYTHONPATH="$CURRENT_TARGET" \
  "$PYTHON_BIN" -B -m alembic heads 2>/dev/null | tail -n 1 | awk '{print $1}'
)"

[[ -n "$ALEMBIC_CURRENT" ]] || fail "Could not resolve current Alembic revision."
[[ -n "$ALEMBIC_HEAD" ]] || fail "Could not resolve Alembic head revision."
[[ "$ALEMBIC_CURRENT" == "$ALEMBIC_HEAD" ]] || fail "Alembic current ($ALEMBIC_CURRENT) does not match head ($ALEMBIC_HEAD)."
pass

if [[ "$REQUIRE_VERIFIED_BACKUP" -eq 1 ]]; then
  "$PYTHON_BIN" -B "$CURRENT_TARGET/tools/platform_backup_restore_drill.py" \
    --output-dir "$SHARED_DIR/backups" \
    --check-latest \
    --max-age-hours "$BACKUP_MAX_AGE_HOURS" >/dev/null 2>/dev/null
  pass
fi

public_status "$PUBLIC_RESULT_STATUS"
