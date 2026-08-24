#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REQUIRE_PREVIOUS=0
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
       [--require-verified-backup] [--require-edge-parity]
       [--backup-max-age-hours <hours>]

Validates the live platform release layout before or after a deploy.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

CURRENT_TARGET="$(readlink -f "$APP_DIR/current" 2>/dev/null || true)"
PREVIOUS_TARGET="$(readlink -f "$APP_DIR/previous" 2>/dev/null || true)"
SHARED_DIR="$APP_DIR/shared"
ENV_FILE="$SHARED_DIR/.env.platform"
PYTHON_BIN="$SHARED_DIR/venv/bin/python"
NODE_BIN="${PLATFORM_NODE_BIN:-$SHARED_DIR/node-v26.3.1/bin/node}"

fail() {
  echo "[FAIL] $1" >&2
  exit 1
}

pass() {
  echo "[OK] $1"
}

load_env_as_data() {
  local safe_env_tool="$SCRIPT_DIR/platform_safe_env_exec.py"
  if [[ ! -f "$safe_env_tool" ]]; then
    safe_env_tool="$CURRENT_TARGET/tools/platform_safe_env_exec.py"
  fi
  [[ -f "$safe_env_tool" && ! -L "$safe_env_tool" ]] \
    || fail "Safe environment parser is missing or unsafe."
  local encoded_assignments
  encoded_assignments="$(
    /usr/bin/python3 -I "$safe_env_tool" export-b64 --path "$ENV_FILE"
  )" || fail "Canonical environment could not be parsed safely."
  local key encoded value
  while IFS=$'\t' read -r key encoded; do
    [[ -n "$key" ]] || continue
    value="$(printf '%s' "$encoded" | /usr/bin/base64 --decode)" \
      || fail "Canonical environment value could not be decoded: $key"
    printf -v "$key" '%s' "$value"
    export "$key"
  done <<<"$encoded_assignments"
}

[[ -n "$CURRENT_TARGET" && -d "$CURRENT_TARGET" ]] || fail "Current release is missing."
pass "Current release: $CURRENT_TARGET"

if [[ "$REQUIRE_PREVIOUS" -eq 1 ]]; then
  [[ -n "$PREVIOUS_TARGET" && -d "$PREVIOUS_TARGET" ]] || fail "Previous release is missing."
fi
if [[ -n "$PREVIOUS_TARGET" && -d "$PREVIOUS_TARGET" ]]; then
  pass "Previous release: $PREVIOUS_TARGET"
else
  echo "[WARN] Previous release is not present."
fi

[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "Shared env file is missing or unsafe: $ENV_FILE"
[[ "$(stat -c '%u:%g:%a:%h' "$ENV_FILE")" == "0:0:600:1" ]] \
  || fail "Canonical env must remain root:root 0600 with one link."
pass "Shared env file present with root-only permissions"

[[ -x "$PYTHON_BIN" ]] || fail "Shared Python runtime is missing: $PYTHON_BIN"
pass "Shared Python runtime present"

[[ -x "$NODE_BIN" ]] || fail "Pinned shared Node runtime is missing: $NODE_BIN"
NODE_VERSION="$("$NODE_BIN" -p "process.versions.node")"
[[ "$NODE_VERSION" == "$EXPECTED_NODE_VERSION" ]] \
  || fail "Node runtime must be exactly $EXPECTED_NODE_VERSION; got $NODE_VERSION."
pass "Pinned shared Node runtime present: v$NODE_VERSION"

for required_path in \
  "$CURRENT_TARGET/RELEASE.json" \
  "$CURRENT_TARGET/apps/platform_web/.next/standalone/server.js" \
  "$CURRENT_TARGET/tools/platform_run_api.sh" \
  "$CURRENT_TARGET/tools/platform_run_worker.sh" \
  "$CURRENT_TARGET/tools/platform_run_web.sh" \
  "$CURRENT_TARGET/tools/platform_run_alembic.sh" \
  "$CURRENT_TARGET/tools/platform_safe_env_exec.py"; do
  [[ -e "$required_path" ]] || fail "Required release file is missing: $required_path"
done
pass "Release artifact files present"

load_env_as_data
pass "Canonical environment parsed as data"

[[ -d "$SHARED_DIR/env" ]] || fail "Rendered service env directory is missing: $SHARED_DIR/env"
RENDER_SERVICE_ENVS_TOOL="$SCRIPT_DIR/platform_render_service_envs.py"
if [[ ! -f "$RENDER_SERVICE_ENVS_TOOL" ]]; then
  RENDER_SERVICE_ENVS_TOOL="$CURRENT_TARGET/tools/platform_render_service_envs.py"
fi
[[ -f "$RENDER_SERVICE_ENVS_TOOL" ]] || fail "Service env renderer is missing."
"$PYTHON_BIN" "$RENDER_SERVICE_ENVS_TOOL" \
  --source "$ENV_FILE" \
  --output-dir "$SHARED_DIR/env" \
  --verify >/dev/null \
  || fail "Rendered service envs are stale or unsafe."
pass "Rendered service envs match canonical configuration"

if [[ "$REQUIRE_EDGE_PARITY" -eq 1 ]]; then
  EDGE_POLICY_TOOL="$SCRIPT_DIR/platform_validate_edge_policy.py"
  if [[ ! -f "$EDGE_POLICY_TOOL" ]]; then
    EDGE_POLICY_TOOL="$CURRENT_TARGET/tools/platform_validate_edge_policy.py"
  fi
  [[ -f "$EDGE_POLICY_TOOL" ]] || fail "Edge policy validator is missing."
  "$PYTHON_BIN" "$EDGE_POLICY_TOOL" \
    --json >/dev/null \
    || fail "Cloudflare/Nginx/UFW trust-range parity check failed."
  pass "Cloudflare/Nginx/UFW trust-range parity passed"
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
  "$PYTHON_BIN" -c \
    "from python_packages.platform_infra.config import get_settings, validate_platform_settings; validate_platform_settings(get_settings(), require_api_secret=True); print('platform-config-ok')"
)"
[[ "$CONFIG_CHECK_OUTPUT" == *"platform-config-ok"* ]] \
  || fail "Production configuration contract validation failed."
pass "Production configuration contract passed"

DB_CHECK_OUTPUT="$(
  cd "$CURRENT_TARGET" && \
  PLATFORM_ENV_FILE="$ENV_FILE" \
  PLATFORM_PYTHON_BIN="$PYTHON_BIN" \
  PYTHONPATH="$CURRENT_TARGET" \
  "$PYTHON_BIN" -c "import asyncio; from python_packages.platform_infra.db import warm_up_engine; asyncio.run(warm_up_engine()); print('platform-db-ok')"
)"
[[ "$DB_CHECK_OUTPUT" == *"platform-db-ok"* ]] || fail "Database warm-up check failed."
pass "Database connectivity check passed"

# Do not route read-only Alembic introspection through the active release's
# shell wrapper: during the transition deployment that wrapper may predate the
# safe dotenv parser. The canonical env is already parsed and exported above.
ALEMBIC_CURRENT="$(
  cd "$CURRENT_TARGET" && \
  PYTHONPATH="$CURRENT_TARGET" \
  "$PYTHON_BIN" -m alembic current 2>/dev/null | tail -n 1 | awk '{print $1}'
)"
ALEMBIC_HEAD="$(
  cd "$CURRENT_TARGET" && \
  PYTHONPATH="$CURRENT_TARGET" \
  "$PYTHON_BIN" -m alembic heads 2>/dev/null | tail -n 1 | awk '{print $1}'
)"

[[ -n "$ALEMBIC_CURRENT" ]] || fail "Could not resolve current Alembic revision."
[[ -n "$ALEMBIC_HEAD" ]] || fail "Could not resolve Alembic head revision."
[[ "$ALEMBIC_CURRENT" == "$ALEMBIC_HEAD" ]] || fail "Alembic current ($ALEMBIC_CURRENT) does not match head ($ALEMBIC_HEAD)."
pass "Alembic revision at head: $ALEMBIC_HEAD"

if [[ "$REQUIRE_VERIFIED_BACKUP" -eq 1 ]]; then
  "$PYTHON_BIN" "$CURRENT_TARGET/tools/platform_backup_restore_drill.py" \
    --output-dir "$SHARED_DIR/backups" \
    --check-latest \
    --max-age-hours "$BACKUP_MAX_AGE_HOURS"
  pass "Fresh restore-verified platform backup present"
fi

echo "[OK] Platform release preflight passed"
