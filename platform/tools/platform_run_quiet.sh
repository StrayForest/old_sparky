#!/usr/bin/env bash
set -uo pipefail

if [[ $# -lt 3 || "$2" != "--" ]]; then
  echo "Usage: $0 <label> -- <command> [args...]" >&2
  exit 2
fi

label="$1"
shift 2

log_file="$(mktemp "/tmp/oldsparky-check-XXXXXX.log")"
failure_lines="${PLATFORM_QUIET_FAILURE_LINES:-160}"

"$@" >"$log_file" 2>&1
status=$?

if [[ "$status" -eq 0 ]]; then
  rm -f "$log_file"
  printf '[OK] %s\n' "$label"
  exit 0
fi

printf '[FAIL] %s (exit %s)\n' "$label" "$status" >&2
tail -n "$failure_lines" "$log_file" >&2
printf '[FAIL-LOG] %s\n' "$log_file" >&2
exit "$status"
