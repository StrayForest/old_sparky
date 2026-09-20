#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

ORIGINAL_ARGS=("$@")
APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
EXPECTED_SHA=""
PUBLIC_RELEASE_SLUG="unavailable"
PUBLIC_SOURCE_SHA="unavailable"

public_status() {
  local status="$1"
  local class="$2"
  printf 'RELEASE_GUARD schema=1 status=%s class=%s release_slug=%s source_sha=%s\n' \
    "$status" "$class" "$PUBLIC_RELEASE_SLUG" "$PUBLIC_SOURCE_SHA"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 2; }
      APP_DIR="$2"
      shift 2
      ;;
    --expected-sha)
      [[ $# -ge 2 ]] || { public_status failed argument >&2; exit 2; }
      EXPECTED_SHA="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_release_lock_exec.sh [--app-dir PATH] [--expected-sha SHA] -- COMMAND [ARG...]

Runs one root-only production mutation while holding the canonical
/run/lock/oldsparky-platform-release.lock.
The command is refused while a durable release transaction is pending. With
--expected-sha, the active immutable release must name that exact source commit.
EOF
      exit 0
      ;;
    *)
      public_status failed argument >&2
      exit 2
      ;;
  esac
done

if [[ $# -lt 1 ]]; then
  public_status failed argument >&2
  exit 2
fi
if [[ "$EUID" -ne 0 ]]; then
  public_status failed privilege >&2
  exit 1
fi
if [[ "$APP_DIR" != /* || "$APP_DIR" == "/" ]]; then
  public_status failed argument >&2
  exit 1
fi
if [[ -n "$EXPECTED_SHA" && ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40,64}$ ]]; then
  public_status failed argument >&2
  exit 2
fi
if [[ ! -d "$APP_DIR" || -L "$APP_DIR" ]]; then
  public_status failed layout >&2
  exit 1
fi
APP_DIR="$(readlink -f "$APP_DIR" 2>/dev/null || true)"
if [[ -z "$APP_DIR" ]]; then
  public_status failed layout >&2
  exit 1
fi
SHARED_DIR="$APP_DIR/shared"
TRANSACTION_STATE="$SHARED_DIR/.release-operation.json"
CURRENT_LINK="$APP_DIR/current"

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LOCK_HELPER="$TOOLS_DIR/platform_release_lock.sh"
if [[ ! -f "$LOCK_HELPER" || -L "$LOCK_HELPER" ]]; then
  public_status failed lock >&2
  exit 3
fi
# The lock is acquired before any service or release mutation.  A supervised
# body re-validates its live /usr/bin/flock parent; no release descriptor is
# inherited by this body or any child.
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

for safe_dir in "$APP_DIR" "$SHARED_DIR" "$APP_DIR/releases"; do
  if [[ ! -d "$safe_dir" || -L "$safe_dir" ]]; then
    public_status failed layout >&2
    exit 1
  fi
  owner="$(stat -c %u "$safe_dir" 2>/dev/null || true)"
  mode="$(stat -c %a "$safe_dir" 2>/dev/null || true)"
  if [[ "$owner" != "0" || -z "$mode" ]] || ((8#$mode & 8#022)); then
    public_status failed layout >&2
    exit 1
  fi
done

if [[ -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
  public_status failed pending_operation >&2
  exit 75
fi
if [[ ! -L "$CURRENT_LINK" || "$(stat -c %u "$CURRENT_LINK" 2>/dev/null || true)" != "0" ]]; then
  public_status failed pointer >&2
  exit 1
fi
CURRENT_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
if [[ -z "$CURRENT_RELEASE" || ! -d "$CURRENT_RELEASE" || -L "$CURRENT_RELEASE" \
  || "$(dirname "$CURRENT_RELEASE")" != "$APP_DIR/releases" ]]; then
  public_status failed pointer >&2
  exit 1
fi
PUBLIC_RELEASE_SLUG="$(basename "$CURRENT_RELEASE")"
if [[ ! "$PUBLIC_RELEASE_SLUG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$ ]]; then
  PUBLIC_RELEASE_SLUG="unavailable"
  public_status failed identity >&2
  exit 1
fi
RELEASE_JSON="$CURRENT_RELEASE/RELEASE.json"
if [[ ! -f "$RELEASE_JSON" || -L "$RELEASE_JSON" ]]; then
  public_status failed metadata >&2
  exit 1
fi
if [[ -n "$EXPECTED_SHA" ]]; then
  if ! DEPLOYED_SHA="$(
    /usr/bin/python3 -I - "$RELEASE_JSON" 2>/dev/null <<'PY'
import json
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
value = payload.get("source_git_commit")
if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
    raise SystemExit("active release source commit is missing or invalid")
print(value)
PY
  )"; then
    public_status failed metadata >&2
    exit 1
  fi
  PUBLIC_SOURCE_SHA="$DEPLOYED_SHA"
  if [[ "$DEPLOYED_SHA" != "$EXPECTED_SHA" ]]; then
    PUBLIC_SOURCE_SHA="unavailable"
    public_status failed identity >&2
    exit 3
  fi
fi

exec "$@"
