#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PLATFORM_SHARED_DIR="$ROOT_DIR"
export PLATFORM_ENV_FILE="$ROOT_DIR/.env.platform"
export PLATFORM_PYTHON_BIN="$ROOT_DIR/.venv_platform/bin/python"

# shellcheck source=platform_runtime_common.sh
source "$ROOT_DIR/tools/platform_runtime_common.sh"
platform_require_python
platform_load_env_file

"$PLATFORM_PYTHON_BIN" -c '
from urllib.parse import urlsplit
from python_packages.platform_infra.config import get_settings, validate_platform_settings
settings = get_settings()
validate_platform_settings(settings)
database = urlsplit(settings.platform_database_url).path.lstrip("/")
if settings.platform_environment != "test" or database != "platformdb_test":
    raise SystemExit("Refusing to run tests outside test/platformdb_test.")
'

exec "$PLATFORM_PYTHON_BIN" -m unittest "$@"
