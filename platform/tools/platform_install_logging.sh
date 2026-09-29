#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOURNALD_SRC="$ROOT_DIR/deploy/journald/60-deadlock-platform-retention.conf"
JOURNALD_DEST_DIR="${PLATFORM_JOURNALD_DIR:-/etc/systemd/journald.conf.d}"
RSYSLOG_SRC="$ROOT_DIR/deploy/rsyslog/05-deadlock-platform.conf"
RSYSLOG_DEST_DIR="${PLATFORM_RSYSLOG_DIR:-/etc/rsyslog.d}"
LOGROTATE_SRC_DIR="$ROOT_DIR/deploy/logrotate"
LOGROTATE_DEST_DIR="${PLATFORM_LOGROTATE_DIR:-/etc/logrotate.d}"
SYSTEMCTL_BIN="${PLATFORM_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
SYSTEMCTL_TIMEOUT_BIN="/usr/bin/timeout"
SYSTEMCTL_TIMEOUT_SECONDS=30

if [[ "${EUID}" -ne 0 ]]; then
  echo "Platform logging installation must run as root." >&2
  exit 1
fi
[[ -f "$SYSTEMCTL_BIN" && ! -L "$SYSTEMCTL_BIN" ]] || {
  echo "Platform logging systemctl path is unavailable." >&2
  exit 1
}
[[ "$(stat -c '%F:%u:%g:%h:%a' -- "$SYSTEMCTL_BIN" 2>/dev/null)" == "regular file:0:0:1:755" ]] || {
  echo "Platform logging systemctl path metadata is unsafe." >&2
  exit 1
}
[[ "$(readlink -f -- "$SYSTEMCTL_BIN" 2>/dev/null)" == "$SYSTEMCTL_BIN" ]] || {
  echo "Platform logging systemctl path resolves through a link." >&2
  exit 1
}

run_systemctl() {
  "$SYSTEMCTL_TIMEOUT_BIN" --signal=TERM --kill-after=5s \
    "${SYSTEMCTL_TIMEOUT_SECONDS}s" "$SYSTEMCTL_BIN" "$@"
}

read_active_state() {
  local unit="$1" output status
  if output="$(run_systemctl is-active "$unit" 2>/dev/null)"; then
    status=0
  else
    status=$?
  fi
  case "$status:$output" in
    0:active) return 0 ;;
    3:inactive) return 3 ;;
    *)
      echo "Refusing logging reload: invalid is-active result for $unit" >&2
      return 4
      ;;
  esac
}

reload_if_active() {
  local unit="$1" active_status=0
  if read_active_state "$unit"; then
    active_status=0
  else
    active_status=$?
  fi
  case "$active_status" in
    0) run_systemctl try-reload-or-restart "$unit" ;;
    3) ;;
    *)
      echo "Refusing logging reload: cannot establish state for $unit" >&2
      return 1
      ;;
  esac
}

install -d -m 0755 "$JOURNALD_DEST_DIR" "$RSYSLOG_DEST_DIR" "$LOGROTATE_DEST_DIR"
install -m 0644 "$JOURNALD_SRC" \
  "$JOURNALD_DEST_DIR/60-deadlock-platform-retention.conf"
install -m 0644 "$RSYSLOG_SRC" \
  "$RSYSLOG_DEST_DIR/05-deadlock-platform.conf"
install -m 0644 "$LOGROTATE_SRC_DIR/nginx" "$LOGROTATE_DEST_DIR/nginx"
install -m 0644 "$LOGROTATE_SRC_DIR/rsyslog" "$LOGROTATE_DEST_DIR/rsyslog"
install -m 0644 "$LOGROTATE_SRC_DIR/btmp" "$LOGROTATE_DEST_DIR/btmp"
# Own the package-provided UFW rule so its weekly rule cannot collide with the
# platform's size-bounded policy. The path is intentionally the same package
# path; no second rule for /var/log/ufw.log is loaded.
install -m 0644 "$LOGROTATE_SRC_DIR/ufw" "$LOGROTATE_DEST_DIR/ufw"

# A release may be installed in a test destination; only touch host daemons
# when the real system configuration is being activated.
if [[ "$JOURNALD_DEST_DIR" == "/etc/systemd/journald.conf.d" ]]; then
  reload_if_active systemd-journald.service
fi
if [[ "$RSYSLOG_DEST_DIR" == "/etc/rsyslog.d" ]]; then
  reload_if_active rsyslog.service
fi

cat <<EOF
Platform logging policy installed.

Journald:  $JOURNALD_DEST_DIR/60-deadlock-platform-retention.conf
Rsyslog:   $RSYSLOG_DEST_DIR/05-deadlock-platform.conf
Logrotate: $LOGROTATE_DEST_DIR/{nginx,rsyslog,ufw,btmp}
EOF
