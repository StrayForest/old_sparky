#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d .venv_platform ]]; then
  python3 -m venv .venv_platform
fi

.venv_platform/bin/pip install -r requirements-platform.txt

if [[ ! -f .env.platform ]]; then
  cp .env.platform.example .env.platform
fi

(
  cd apps/platform_web
  "$ROOT_DIR/tools/platform_web_npm.sh" ci
)

cat <<'EOF'
Platform bootstrap complete.

Next steps:
1. Review and adjust platform/.env.platform.
   PLATFORM_DATABASE_URL must point to the dedicated platformdb database, not the bot's sparkydb.
2. Apply migrations with:
   cd platform && tools/platform_run_alembic.sh upgrade head
3. Prepare an isolated local test runtime before running tests:
   python tools/platform_prepare_test_runtime.py --apply
   tools/platform_run_alembic.sh upgrade head
   .venv_platform/bin/python tools/platform_verify.py backend
4. Start the API:
   platform/tools/platform_run_api.sh
5. Start the worker:
   platform/tools/platform_run_worker.sh
6. Start the web app:
   platform/tools/platform_run_web.sh
   Frontend sources and build output live under platform/apps/platform_web.
EOF
