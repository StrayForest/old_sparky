#!/usr/bin/env bash
# Functions sourced by the external fixture supervisor. The observer is
# launched in its own session so shutdown can address only its process group.

platform_external_fixture_stop_observer_group() {
  local observer_leader="${1:-}"
  local term_grace_seconds="${2:-30}"
  local kill_grace_seconds="${3:-5}"
  local waited=0

  [[ "$observer_leader" =~ ^[1-9][0-9]{0,8}$ ]] || return 1
  [[ "$term_grace_seconds" =~ ^[0-9]{1,2}$ ]] \
    && (( term_grace_seconds <= 30 )) || return 1
  [[ "$kill_grace_seconds" =~ ^[0-9]$ ]] \
    && (( kill_grace_seconds <= 5 )) || return 1

  # The caller created this session specifically for the observer timeout
  # tree. Signal the full group, then reap its direct timeout leader before
  # checking whether any descendant ignored TERM.
  if kill -0 -- "-$observer_leader" 2>/dev/null; then
    kill -TERM -- "-$observer_leader" 2>/dev/null || true
  fi
  wait "$observer_leader" 2>/dev/null || true

  while kill -0 -- "-$observer_leader" 2>/dev/null \
    && (( waited < term_grace_seconds )); do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 -- "-$observer_leader" 2>/dev/null; then
    kill -KILL -- "-$observer_leader" 2>/dev/null || true
  fi

  waited=0
  while kill -0 -- "-$observer_leader" 2>/dev/null \
    && (( waited < kill_grace_seconds )); do
    sleep 1
    waited=$((waited + 1))
  done
  kill -0 -- "-$observer_leader" 2>/dev/null && return 1

  return 0
}
