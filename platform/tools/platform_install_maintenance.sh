#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_SRC_DIR="$ROOT_DIR/deploy/systemd"
SYSTEMD_DEST_DIR="${PLATFORM_SYSTEMD_DIR:-/etc/systemd/system}"
JOURNALD_SRC="$ROOT_DIR/deploy/journald/60-deadlock-platform-retention.conf"
JOURNALD_DEST_DIR="${PLATFORM_JOURNALD_DIR:-/etc/systemd/journald.conf.d}"
APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Platform maintenance installation must run as root." >&2
  exit 1
fi

install -m 0644 "$SYSTEMD_SRC_DIR/deadlock-maintenance.service" \
  "$SYSTEMD_DEST_DIR/deadlock-maintenance.service"
install -m 0644 "$SYSTEMD_SRC_DIR/deadlock-maintenance.timer" \
  "$SYSTEMD_DEST_DIR/deadlock-maintenance.timer"
install -d -m 0755 "$JOURNALD_DEST_DIR"
install -m 0644 "$JOURNALD_SRC" \
  "$JOURNALD_DEST_DIR/60-deadlock-platform-retention.conf"

if [[ -f "$APP_DIR/shared/.env.platform" ]]; then
  chmod 0600 "$APP_DIR/shared/.env.platform"
fi

systemctl daemon-reload
systemctl enable --now deadlock-maintenance.timer
systemctl restart systemd-journald
journalctl --rotate
journalctl --vacuum-time=30d
journalctl --vacuum-size=512M

cat <<EOF
Platform maintenance installed.

Timer:
  deadlock-maintenance.timer
Journal policy:
  $JOURNALD_DEST_DIR/60-deadlock-platform-retention.conf

Run and inspect now:
  systemctl start deadlock-maintenance.service
  systemctl status deadlock-maintenance.service --no-pager
EOF
