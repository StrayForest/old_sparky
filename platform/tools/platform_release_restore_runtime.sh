#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

ORIGINAL_ARGS=("$@")
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
SYSTEMD_STATE=""
SYSTEMCTL_BIN="/usr/bin/systemctl"
PUBLIC_RELEASE_SLUG="unavailable"
PUBLIC_SOURCE_SHA="unavailable"

public_status() {
  local status="$1"
  local class="${2:-runtime}"
  printf 'RELEASE_RUNTIME schema=1 status=%s class=%s release_slug=%s source_sha=%s\n' \
    "$status" "$class" "$PUBLIC_RELEASE_SLUG" "$PUBLIC_SOURCE_SHA"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      APP_DIR="$2"
      shift 2
      ;;
    --release)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
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
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      EXPECTED_CSP_MODE="$2"
      shift 2
      ;;
    --edge-origin)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      EDGE_ORIGIN="$2"
      shift 2
      ;;
    --edge-host)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      EDGE_HOST="$2"
      shift 2
      ;;
    --public-edge-origin)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      PUBLIC_EDGE_ORIGIN="$2"
      shift 2
      ;;
    --systemd-state)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      SYSTEMD_STATE="$2"
      shift 2
      ;;
    --systemctl)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 1; }
      SYSTEMCTL_BIN="$2"
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
      public_status failed argument >&2
      exit 1
      ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  public_status failed privilege >&2
  exit 1
fi
if [[ "$APP_DIR" != /* || "$APP_DIR" == "/" ]]; then
  public_status failed argument >&2
  exit 1
fi
TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LOCK_HELPER="$TOOLS_DIR/platform_release_lock.sh"
if [[ ! -f "$LOCK_HELPER" || -L "$LOCK_HELPER" ]]; then
  public_status failed lock >&2
  exit 3
fi
# Runtime restore may be called directly or as part of rollback/recovery.  In
# both cases it must use the one canonical release lock; a supervised body
# re-validates its live /usr/bin/flock parent across readiness checks.
# shellcheck source=/dev/null
source "$LOCK_HELPER"
platform_release_lock_supervise "${ORIGINAL_ARGS[@]}" || {
  lock_status=$?
  if [[ "$lock_status" -eq "$PLATFORM_RELEASE_LOCK_CONFLICT_EXIT_CODE" ]]; then
    public_status failed lock >&2
    exit 3
  fi
  exit "$lock_status"
}
if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" != "1" ]]; then
  exit 0
fi
if ! platform_release_lock_open; then
  public_status failed lock >&2
  exit 3
fi
trap platform_release_lock_close EXIT
if [[ -z "$RELEASE" || "$RELEASE" != /* || ! -d "$RELEASE" || -L "$RELEASE" ]]; then
  public_status failed layout >&2
  exit 1
fi
if [[ ! -d "$APP_DIR/shared" || -L "$APP_DIR/shared" ]]; then
  public_status failed layout >&2
  exit 1
fi

RELEASE="$(readlink -f "$RELEASE" 2>/dev/null || true)"
RELEASES_DIR="$(readlink -f "$APP_DIR/releases" 2>/dev/null || true)"
if [[ "$(dirname "$RELEASE")" != "$RELEASES_DIR" ]]; then
  public_status failed layout >&2
  exit 1
fi
if [[ ! "$RELEASE" =~ /[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
  public_status failed layout >&2
  exit 1
fi

PUBLIC_RELEASE_SLUG="$(basename "$RELEASE")"
PUBLIC_SOURCE_SHA="unavailable"
source_sha="$({
  /usr/bin/python3 -I - "$RELEASE/RELEASE.json" <<'PY'
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
[[ -n "$source_sha" ]] && PUBLIC_SOURCE_SHA="$source_sha"

on_exit() {
  local exit_status="$?"
  trap - EXIT
  if [[ "$exit_status" -ne 0 ]]; then
    public_status failed >&2
  fi
  platform_release_lock_close
  exit "$exit_status"
}
trap on_exit EXIT

UNITS_TOOL="$RELEASE/tools/platform_install_systemd_units.sh"
NGINX_TOOL="$RELEASE/tools/platform_install_nginx.py"
SMOKE_TOOL="$RELEASE/tools/platform_deploy_smoke.py"
SYSTEMD_STATE_TOOL="$TOOLS_DIR/platform_release_systemd_state.py"
SHARED_VENV="$APP_DIR/shared/venv"
if [[ ! -x "$UNITS_TOOL" || ! -f "$NGINX_TOOL" || ! -x "$SHARED_VENV/bin/python" ]]; then
  public_status failed tooling >&2
  exit 1
fi
if [[ -n "$SYSTEMD_STATE" ]]; then
  if [[ ! -f "$SYSTEMD_STATE" || -L "$SYSTEMD_STATE" || ! -x "$SYSTEMD_STATE_TOOL" ]]; then
    public_status failed systemd_state >&2
    exit 1
  fi
fi
if [[ "$RUN_SMOKE" -eq 1 && ! -f "$SMOKE_TOOL" ]]; then
  public_status failed tooling >&2
  exit 1
fi
LIVE_QA_RUNTIME_INSTALLER="$RELEASE/tools/platform_live_qa_runtime_install.py"
if [[ ! -f "$LIVE_QA_RUNTIME_INSTALLER" || -L "$LIVE_QA_RUNTIME_INSTALLER" ]]; then
  public_status failed liveqa_runtime >&2
  exit 1
fi
# Rollback/recovery uses this same path, so reconcile the digest-bound
# generation before units, Nginx, readiness or smoke can observe the restored
# release. The canonical release lock remains held by this script.
"$SHARED_VENV/bin/python" -I "$LIVE_QA_RUNTIME_INSTALLER" \
  reconcile --app-dir "$APP_DIR" >/dev/null 2>/dev/null

prepare_runtime_private() {
  # Restoring unit files is a data-plane operation.  The installer defaults
  # to enable/start for a first activation, so every rollback/recovery call
  # must override that default explicitly before it can touch systemd.
  export PLATFORM_ENABLE_SYSTEMD_UNITS=0
  PLATFORM_APP_DIR="$APP_DIR" "$UNITS_TOOL"
  set +e
  PLATFORM_APP_DIR="$APP_DIR" "$SHARED_VENV/bin/python" \
    "$NGINX_TOOL" --apply --reload --json
  nginx_status="$?"
  set -e
}

restore_systemd_enabled_state() {
  [[ -n "$SYSTEMD_STATE" ]] || return 0
  "$SHARED_VENV/bin/python" -I "$SYSTEMD_STATE_TOOL" \
    restore-enabled --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
    --systemctl "$SYSTEMCTL_BIN" \
    >/dev/null 2>/dev/null
}

restore_systemd_state() {
  [[ -n "$SYSTEMD_STATE" ]] || return 0
  "$SHARED_VENV/bin/python" -I "$SYSTEMD_STATE_TOOL" \
    restore --state "$SYSTEMD_STATE" --app-dir "$APP_DIR" \
    --systemctl "$SYSTEMCTL_BIN" \
    >/dev/null 2>/dev/null
}

if [[ "$PREPARE_RUNTIME" -eq 1 ]]; then
  prepare_runtime_private >/dev/null 2>/dev/null
  if [[ "$nginx_status" -ne 0 ]]; then
    # The installer restores its disk snapshots on failure. Validate and reload
    # that restored disk state before returning failure so active Nginx cannot
    # remain divergent from the recovery contour.
    /usr/sbin/nginx -t >/dev/null 2>/dev/null
    /usr/bin/systemctl reload nginx.service >/dev/null 2>/dev/null
    exit "$nginx_status"
  fi
  # --prepare-only must never start an inactive unit.  It may repair the
  # enablement symlinks recorded by the rollback receipt, without touching
  # active state; the later restart-only phase restores both dimensions.
  restore_systemd_enabled_state
fi

if [[ "$RUN_RESTART" -eq 1 && "$RESTART_AFTER" -eq 1 ]]; then
  if [[ -n "$SYSTEMD_STATE" ]]; then
    restore_systemd_state
  else
    /usr/bin/systemctl restart deadlock-api deadlock-worker deadlock-web >/dev/null 2>/dev/null
    for service in deadlock-api deadlock-worker deadlock-web; do
      /usr/bin/systemctl is-active --quiet "$service" >/dev/null 2>/dev/null
    done
  fi
  if /usr/bin/systemctl is-active --quiet deadlock-api >/dev/null 2>/dev/null; then
    /usr/bin/curl --fail --silent --show-error --max-time 10 \
      http://127.0.0.1:8010/api/v1/health/ready >/dev/null 2>/dev/null
  fi
  if /usr/bin/systemctl is-active --quiet deadlock-web >/dev/null 2>/dev/null; then
    /usr/bin/curl --fail --silent --show-error --max-time 10 \
      http://127.0.0.1:3000/ >/dev/null 2>/dev/null
  fi
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
      --expected-csp-mode "$EXPECTED_CSP_MODE" >/dev/null 2>/dev/null
  PLATFORM_ENV_FILE="$APP_DIR/shared/.env.platform" \
    PLATFORM_PYTHON_BIN="$SHARED_VENV/bin/python" \
    PLATFORM_SHARED_DIR="$APP_DIR/shared" \
    PLATFORM_APP_DIR="$APP_DIR" \
    "$SHARED_VENV/bin/python" "$SMOKE_TOOL" \
      --app-dir "$APP_DIR" \
      --env-file "$APP_DIR/shared/.env.platform" \
      --edge-origin "$PUBLIC_EDGE_ORIGIN" \
      --expected-csp-mode "$EXPECTED_CSP_MODE" >/dev/null 2>/dev/null
fi

public_status restored
