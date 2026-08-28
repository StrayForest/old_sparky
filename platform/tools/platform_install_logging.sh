#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOURNALD_SRC="$ROOT_DIR/deploy/journald/60-deadlock-platform-retention.conf"
JOURNALD_DEST_DIR="${PLATFORM_JOURNALD_DIR:-/etc/systemd/journald.conf.d}"
RSYSLOG_SRC="$ROOT_DIR/deploy/rsyslog/05-deadlock-platform.conf"
RSYSLOG_DEST_DIR="${PLATFORM_RSYSLOG_DIR:-/etc/rsyslog.d}"
LOGROTATE_SRC_DIR="$ROOT_DIR/deploy/logrotate"
LOGROTATE_DEST_DIR="${PLATFORM_LOGROTATE_DIR:-/etc/logrotate.d}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Platform logging installation must run as root." >&2
  exit 1
fi

install -d -m 0755 "$JOURNALD_DEST_DIR" "$RSYSLOG_DEST_DIR" "$LOGROTATE_DEST_DIR"
install -m 0644 "$JOURNALD_SRC" \
  "$JOURNALD_DEST_DIR/60-deadlock-platform-retention.conf"
install -m 0644 "$RSYSLOG_SRC" \
  "$RSYSLOG_DEST_DIR/05-deadlock-platform.conf"
install -m 0644 "$LOGROTATE_SRC_DIR/nginx" "$LOGROTATE_DEST_DIR/nginx"
install -m 0644 "$LOGROTATE_SRC_DIR/rsyslog" "$LOGROTATE_DEST_DIR/rsyslog"
install -m 0644 "$LOGROTATE_SRC_DIR/btmp" "$LOGROTATE_DEST_DIR/btmp"

# A release may be installed in a test destination; only touch host daemons
# when the real system configuration is being activated.
if [[ "$JOURNALD_DEST_DIR" == "/etc/systemd/journald.conf.d" ]]; then
  if systemctl is-active --quiet systemd-journald.service; then
    systemctl try-reload-or-restart systemd-journald.service
  fi
fi
if [[ "$RSYSLOG_DEST_DIR" == "/etc/rsyslog.d" ]]; then
  if systemctl is-active --quiet rsyslog.service; then
    systemctl try-reload-or-restart rsyslog.service
  fi
fi

cat <<EOF
Platform logging policy installed.

Journald:  $JOURNALD_DEST_DIR/60-deadlock-platform-retention.conf
Rsyslog:   $RSYSLOG_DEST_DIR/05-deadlock-platform.conf
Logrotate: $LOGROTATE_DEST_DIR/{nginx,rsyslog,btmp}
EOF
