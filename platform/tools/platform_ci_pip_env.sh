#!/usr/bin/env bash
set -euo pipefail

# Execute a pip/toolchain command with no ambient pip configuration.  All
# callers pass absolute executable paths, so a fixed minimal PATH is enough;
# no PIP_*, proxy, certificate or user-home variables cross this boundary.
[[ "$#" -gt 0 ]] || {
  echo "CI pip environment wrapper requires a command" >&2
  exit 2
}

exec /usr/bin/env -i \
  PATH=/usr/bin:/bin \
  LANG=C \
  LC_ALL=C \
  PIP_CONFIG_FILE=/dev/null \
  "$@"
