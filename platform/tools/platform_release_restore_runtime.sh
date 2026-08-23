#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
RELEASE=""
RESTART_AFTER=1
RUN_SMOKE=1
PREPARE_RUNTIME=1
RUN_RESTART=1
EXPECTED_CSP_MODE="enforce"
EDGE_ORIGIN="https://127.0.0.1"
EDGE_HOST="old-sparky.com"
PUBLIC_EDGE_ORIGIN="https://old-sparky.com"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || { echo "--app-dir requires a path." >&2; exit 1; }
      APP_DIR="$2"
      shift 2
      ;;
    --release)
      [[ $# -ge 2 ]] || { echo "--release requires a path." >&2; exit 1; }
      RELEASE="$2"
      shift 2
      ;;
    --no-restart)
      RESTART_AFTER=0
      RUN_SMOKE=0
      RUN_RESTART=0
      shift
      ;;
    --prepare-only)
      RESTART_AFTER=0
      RUN_SMOKE=0
      RUN_RESTART=0
      shift
      ;;
    --restart-only)
      PREPARE_RUNTIME=0
      RUN_SMOKE=0
      shift
      ;;
    --smoke-only)
      PREPARE_RUNTIME=0
      RESTART_AFTER=0
      RUN_RESTART=0
      shift
      ;;
    --expected-csp-mode)
      [[ $# -ge 2 ]] || { echo "--expected-csp-mode requires a value." >&2; exit 1; }
      EXPECTED_CSP_MODE="$2"
      shift 2
      ;;
    --edge-origin)
      [[ $# -ge 2 ]] || { echo "--edge-origin requires an origin." >&2; exit 1; }
      EDGE_ORIGIN="$2"
      shift 2
      ;;
    --edge-host)
      [[ $# -ge 2 ]] || { echo "--edge-host requires a hostname." >&2; exit 1; }
      EDGE_HOST="$2"
      shift 2
      ;;
    --public-edge-origin)
      [[ $# -ge 2 ]] || { echo "--public-edge-origin requires an origin." >&2; exit 1; }
      PUBLIC_EDGE_ORIGIN="$2"
      shift 2
      ;;
    --skip-smoke)
      RUN_SMOKE=0
      shift
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_release_restore_runtime.sh --release <release-dir> [options]

Installs the release-specific systemd units and Nginx configuration before
optionally restarting services and running readiness/smoke checks. It is used
while a durable release receipt remains retained by the caller.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  echo "Release runtime restoration requires root." >&2
  exit 1
fi
if [[ "$APP_DIR" != /* || "$APP_DIR" == "/" ]]; then
  echo "Application directory must be an absolute non-root path." >&2
  exit 1
fi
if [[ -z "$RELEASE" || "$RELEASE" != /* || ! -d "$RELEASE" || -L "$RELEASE" ]]; then
  echo "Release directory is missing or unsafe: $RELEASE" >&2
  exit 1
fi
if [[ ! -d "$APP_DIR/shared" || -L "$APP_DIR/shared" ]]; then
  echo "Shared release directory is missing or unsafe: $APP_DIR/shared" >&2
  exit 1
fi

RELEASE="$(readlink -f "$RELEASE")"
RELEASES_DIR="$(readlink -f "$APP_DIR/releases")"
if [[ "$(dirname "$RELEASE")" != "$RELEASES_DIR" ]]; then
  echo "Release directory is outside the installed releases: $RELEASE" >&2
  exit 1
fi
if [[ ! "$RELEASE" =~ /[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
  echo "Release directory name is unsafe: $RELEASE" >&2
  exit 1
fi

UNITS_TOOL="$RELEASE/tools/platform_install_systemd_units.sh"
NGINX_TOOL="$RELEASE/tools/platform_install_nginx.py"
SMOKE_TOOL="$RELEASE/tools/platform_deploy_smoke.py"
SHARED_VENV="$APP_DIR/shared/venv"
if [[ ! -x "$UNITS_TOOL" || ! -f "$NGINX_TOOL" || ! -x "$SHARED_VENV/bin/python" ]]; then
  echo "Release runtime tools or restored Python are unavailable: $RELEASE" >&2
  exit 1
fi
if [[ "$RUN_SMOKE" -eq 1 && ! -f "$SMOKE_TOOL" ]]; then
  echo "Release smoke tool is unavailable: $SMOKE_TOOL" >&2
  exit 1
fi

if [[ "$PREPARE_RUNTIME" -eq 1 ]]; then
  PLATFORM_APP_DIR="$APP_DIR" "$UNITS_TOOL"
  PLATFORM_APP_DIR="$APP_DIR" "$SHARED_VENV/bin/python" \
    "$NGINX_TOOL" --apply --reload --json
fi

if [[ "$RUN_RESTART" -eq 1 && "$RESTART_AFTER" -eq 1 ]]; then
  /usr/bin/systemctl restart deadlock-api deadlock-worker deadlock-web
  for service in deadlock-api deadlock-worker deadlock-web; do
    /usr/bin/systemctl is-active --quiet "$service"
  done
  /usr/bin/curl --fail --silent --show-error --max-time 10 \
    http://127.0.0.1:8010/api/v1/health/ready >/dev/null
  /usr/bin/curl --fail --silent --show-error --max-time 10 \
    http://127.0.0.1:3000/ >/dev/null
fi

if [[ "$RUN_SMOKE" -eq 1 ]]; then
  PLATFORM_ENV_FILE="$APP_DIR/shared/.env.platform" \
    PLATFORM_PYTHON_BIN="$SHARED_VENV/bin/python" \
    PLATFORM_SHARED_DIR="$APP_DIR/shared" \
    PLATFORM_APP_DIR="$APP_DIR" \
    "$SHARED_VENV/bin/python" "$SMOKE_TOOL" \
      --app-dir "$APP_DIR" \
      --env-file "$APP_DIR/shared/.env.platform" \
      --edge-origin "$EDGE_ORIGIN" \
      --edge-host "$EDGE_HOST" \
      --edge-insecure-loopback \
      --expected-csp-mode "$EXPECTED_CSP_MODE"
  PLATFORM_ENV_FILE="$APP_DIR/shared/.env.platform" \
    PLATFORM_PYTHON_BIN="$SHARED_VENV/bin/python" \
    PLATFORM_SHARED_DIR="$APP_DIR/shared" \
    PLATFORM_APP_DIR="$APP_DIR" \
    "$SHARED_VENV/bin/python" "$SMOKE_TOOL" \
      --app-dir "$APP_DIR" \
      --env-file "$APP_DIR/shared/.env.platform" \
      --edge-origin "$PUBLIC_EDGE_ORIGIN" \
      --expected-csp-mode "$EXPECTED_CSP_MODE"
fi

echo "Release runtime restored: $RELEASE"
