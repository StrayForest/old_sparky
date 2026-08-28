#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_SRC_DIR="$ROOT_DIR/deploy/systemd"
SYSTEMD_DEST_DIR="${PLATFORM_SYSTEMD_DIR:-/etc/systemd/system}"
APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Platform maintenance installation must run as root." >&2
  exit 1
fi

install -m 0644 "$SYSTEMD_SRC_DIR/deadlock-maintenance.service" \
  "$SYSTEMD_DEST_DIR/deadlock-maintenance.service"
install -m 0644 "$SYSTEMD_SRC_DIR/deadlock-maintenance.timer" \
  "$SYSTEMD_DEST_DIR/deadlock-maintenance.timer"
install -m 0644 "$SYSTEMD_SRC_DIR/deadlock-offsite-backup.service" \
  "$SYSTEMD_DEST_DIR/deadlock-offsite-backup.service"
install -m 0644 "$SYSTEMD_SRC_DIR/deadlock-offsite-backup.timer" \
  "$SYSTEMD_DEST_DIR/deadlock-offsite-backup.timer"
install -m 0644 "$SYSTEMD_SRC_DIR/deadlock-logrotate.service" \
  "$SYSTEMD_DEST_DIR/deadlock-logrotate.service"
install -m 0644 "$SYSTEMD_SRC_DIR/deadlock-logrotate.timer" \
  "$SYSTEMD_DEST_DIR/deadlock-logrotate.timer"

"$ROOT_DIR/tools/platform_install_logging.sh"

if [[ -f "$APP_DIR/shared/.env.platform" ]]; then
  chmod 0600 "$APP_DIR/shared/.env.platform"
fi

systemctl daemon-reload
systemctl enable --now deadlock-maintenance.timer deadlock-logrotate.timer
journalctl --rotate
journalctl --vacuum-time=30d
journalctl --vacuum-size=256M

cat <<EOF
Platform maintenance installed.

Timer:
  deadlock-maintenance.timer
Journal policy:
  /etc/systemd/journald.conf.d/60-deadlock-platform-retention.conf

Run and inspect now:
  systemctl start deadlock-maintenance.service
  systemctl status deadlock-maintenance.service --no-pager
EOF
