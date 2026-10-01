#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_SRC_DIR="$ROOT_DIR/deploy/systemd"
SYSTEMD_DEST_DIR="${PLATFORM_SYSTEMD_DIR:-/etc/systemd/system}"
APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
SYSTEMCTL_BIN="${PLATFORM_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
JOURNALCTL_BIN="${PLATFORM_JOURNALCTL_BIN:-/usr/bin/journalctl}"
SYSTEMD_TIMEOUT_BIN="/usr/bin/timeout"
SYSTEMD_TIMEOUT_SECONDS=30

# Keep maintenance manager calls under the same bounded helper contract as the
# normal unit installer.  A wedged systemd/journald manager must fail the
# installation promptly and leave the caller's durable release state intact.
run_systemd_command() {
  "$SYSTEMD_TIMEOUT_BIN" --signal=TERM --kill-after=5s \
    "${SYSTEMD_TIMEOUT_SECONDS}s" "$@"
}

run_systemctl() {
  run_systemd_command "$SYSTEMCTL_BIN" "$@"
}

run_journalctl() {
  run_systemd_command "$JOURNALCTL_BIN" "$@"
}

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

run_systemctl daemon-reload
run_systemctl enable --now deadlock-maintenance.timer deadlock-logrotate.timer
run_journalctl --rotate
run_journalctl --vacuum-time=30d
run_journalctl --vacuum-size=256M

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
