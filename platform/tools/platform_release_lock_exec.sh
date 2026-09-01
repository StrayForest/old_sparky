#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
EXPECTED_SHA=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || { echo "--app-dir requires a path." >&2; exit 2; }
      APP_DIR="$2"
      shift 2
      ;;
    --expected-sha)
      [[ $# -ge 2 ]] || { echo "--expected-sha requires a commit SHA." >&2; exit 2; }
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

Runs one root-only production mutation while holding the platform release lock.
The command is refused while a durable release transaction is pending. With
--expected-sha, the active immutable release must name that exact source commit.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ $# -lt 1 ]]; then
  echo "A command is required after --." >&2
  exit 2
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "Production mutation guard requires root." >&2
  exit 1
fi
if [[ "$APP_DIR" != /* || "$APP_DIR" == "/" ]]; then
  echo "Application directory must be an absolute non-root path." >&2
  exit 1
fi
if [[ -n "$EXPECTED_SHA" && ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40,64}$ ]]; then
  echo "Expected source SHA is invalid." >&2
  exit 2
fi
if [[ ! -d "$APP_DIR" || -L "$APP_DIR" ]]; then
  echo "Application directory is missing or unsafe: $APP_DIR" >&2
  exit 1
fi
APP_DIR="$(readlink -f "$APP_DIR")"
SHARED_DIR="$APP_DIR/shared"
TRANSACTION_STATE="$SHARED_DIR/.release-operation.json"
CURRENT_LINK="$APP_DIR/current"

for safe_dir in "$APP_DIR" "$SHARED_DIR" "$APP_DIR/releases"; do
  if [[ ! -d "$safe_dir" || -L "$safe_dir" ]]; then
    echo "Production guard directory is missing or unsafe: $safe_dir" >&2
    exit 1
  fi
  owner="$(stat -c %u "$safe_dir")"
  mode="$(stat -c %a "$safe_dir")"
  if [[ "$owner" != "0" || $((8#$mode & 8#022)) -ne 0 ]]; then
    echo "Production guard directory permissions are unsafe: $safe_dir" >&2
    exit 1
  fi
done

if [[ "${PLATFORM_RELEASE_GUARD_HELD:-0}" != "1" ]]; then
  forwarded=(--app-dir "$APP_DIR")
  if [[ -n "$EXPECTED_SHA" ]]; then
    forwarded+=(--expected-sha "$EXPECTED_SHA")
  fi
  forwarded+=(-- "$@")
  exec /usr/bin/flock -n -E 75 "$SHARED_DIR" \
    /usr/bin/env PLATFORM_RELEASE_GUARD_HELD=1 \
    /bin/bash "$0" "${forwarded[@]}"
fi

if [[ -e "$TRANSACTION_STATE" || -L "$TRANSACTION_STATE" ]]; then
  echo "Production mutation refused while a release transaction is pending." >&2
  exit 75
fi
if [[ ! -L "$CURRENT_LINK" || "$(stat -c %u "$CURRENT_LINK")" != "0" ]]; then
  echo "Active release pointer is missing or unsafe." >&2
  exit 1
fi
CURRENT_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
if [[ -z "$CURRENT_RELEASE" || ! -d "$CURRENT_RELEASE" || -L "$CURRENT_RELEASE" \
  || "$(dirname "$CURRENT_RELEASE")" != "$APP_DIR/releases" ]]; then
  echo "Active release target is missing or outside the release directory." >&2
  exit 1
fi
RELEASE_JSON="$CURRENT_RELEASE/RELEASE.json"
if [[ ! -f "$RELEASE_JSON" || -L "$RELEASE_JSON" ]]; then
  echo "Active release metadata is missing or unsafe." >&2
  exit 1
fi
if [[ -n "$EXPECTED_SHA" ]]; then
  DEPLOYED_SHA="$(
    /usr/bin/python3 -I - "$RELEASE_JSON" <<'PY'
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
  )"
  if [[ "$DEPLOYED_SHA" != "$EXPECTED_SHA" ]]; then
    echo "Production mutation refused: deployed SHA $DEPLOYED_SHA != expected $EXPECTED_SHA." >&2
    exit 3
  fi
fi

exec "$@"
