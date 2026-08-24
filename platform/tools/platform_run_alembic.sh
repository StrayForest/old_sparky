#!/usr/bin/env bash
set -Eeuo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$TOOLS_DIR/platform_runtime_common.sh"

platform_load_env_file
platform_require_python

is_production_upgrade=0
if [[ "${PLATFORM_ENVIRONMENT:-}" == "production" \
  && $# -eq 2 && "$1" == "upgrade" && "$2" == "head" ]]; then
  is_production_upgrade=1
fi

if [[ "$is_production_upgrade" -eq 1 ]]; then
  if [[ "$EUID" -ne 0 ]]; then
    echo "Production Alembic upgrade must run as root inside the release transaction." >&2
    exit 1
  fi
  transaction_state="$PLATFORM_APP_DIR/shared/.release-operation.json"
  transaction_tool="$PLATFORM_ROOT_DIR/tools/platform_release_transaction.py"
  if [[ ! -f "$transaction_state" || -L "$transaction_state" ]]; then
    echo "Production Alembic upgrade requires a durable release transaction." >&2
    exit 1
  fi
  transaction_json="$(
    /usr/bin/python3 -I "$transaction_tool" status \
      --state "$transaction_state" --json
  )"
  readarray -t transaction_fields < <(
    printf '%s' "$transaction_json" | /usr/bin/python3 -I -c '
import json
import sys
record = json.load(sys.stdin)
print(record["operation"])
print(record["phase"])
print(record["app_dir"])
'
  )
  if [[ "${transaction_fields[0]:-}" != "install" \
    || "${transaction_fields[1]:-}" != "migration-pending" \
    || "${transaction_fields[2]:-}" != "$PLATFORM_APP_DIR" ]]; then
    echo "Production Alembic upgrade is outside the migration-pending install phase." >&2
    exit 1
  fi

  # Repeat the complete release preflight after staging, while the deploy
  # wrapper holds the release lock. This closes the preflight->staging TOCTOU
  # window before the first database mutation.
  "$TOOLS_DIR/platform_release_preflight.sh" \
    --app-dir "$PLATFORM_APP_DIR" \
    --require-previous \
    --require-verified-backup \
    --require-edge-parity \
    --backup-max-age-hours 24

  # No old-code writer may overlap a schema migration. The release wrapper
  # restarts these services only after the candidate pointer is activated.
  # On any uncertain migration outcome they deliberately remain stopped.
  /usr/bin/systemctl stop deadlock-api deadlock-worker deadlock-web
  for service in deadlock-api deadlock-worker deadlock-web; do
    if /usr/bin/systemctl is-active --quiet "$service"; then
      echo "Refusing migration while service remains active: $service" >&2
      exit 1
    fi
  done
fi

cd "$PLATFORM_ROOT_DIR"
exec "$PLATFORM_PYTHON_BIN" -m alembic "$@"
