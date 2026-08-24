#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQUIRED_NODE_VERSION="26.3.1"

node_version() {
  "$1" -p "process.versions.node" 2>/dev/null || true
}

select_node() {
  local candidate
  local version

  if [[ -n "${PLATFORM_NODE_BIN:-}" ]]; then
    candidate="$PLATFORM_NODE_BIN"
    version="$(node_version "$candidate")"
    if [[ -x "$candidate" && "$version" == "$REQUIRED_NODE_VERSION" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
    echo "PLATFORM_NODE_BIN must point to Node ${REQUIRED_NODE_VERSION}; got ${candidate} (${version:-unavailable})." >&2
    return 1
  fi

  for candidate in \
    "$ROOT_DIR/node-v26.3.1/bin/node" \
    "$ROOT_DIR/node-current/bin/node" \
    "/opt/oldsparky/platform/shared/node-v26.3.1/bin/node" \
    "/opt/oldsparky/platform/shared/node-current/bin/node" \
    "$(command -v node 2>/dev/null || true)"; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    version="$(node_version "$candidate")"
    if [[ "$version" == "$REQUIRED_NODE_VERSION" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "Node ${REQUIRED_NODE_VERSION} is required. Install it or set PLATFORM_NODE_BIN." >&2
  return 1
}

NODE_BIN="$(select_node)"

if [[ "${1:-}" == "--npm" ]]; then
  shift
  NPM_BIN="$(dirname "$NODE_BIN")/npm"
  if [[ ! -x "$NPM_BIN" ]]; then
    echo "npm is missing next to ${NODE_BIN}." >&2
    exit 1
  fi
  NPM_CLI="$(readlink -f "$NPM_BIN")"
  exec "$NODE_BIN" "$NPM_CLI" "$@"
fi

exec "$NODE_BIN" "$@"
