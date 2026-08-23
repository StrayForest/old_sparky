#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_SRC_DIR="$ROOT_DIR/deploy/systemd"
SYSTEMD_DEST_DIR="${PLATFORM_SYSTEMD_DIR:-/etc/systemd/system}"
APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
ENABLE_SYSTEMD_UNITS="${PLATFORM_ENABLE_SYSTEMD_UNITS:-1}"

for unit_name in \
  deadlock-api.service \
  deadlock-worker.service \
  deadlock-web.service \
  deadlock-maintenance.service \
  deadlock-maintenance.timer \
  deadlock-offsite-backup.service \
  deadlock-offsite-backup.timer \
  deadlock-cloudflare-ips.service \
  deadlock-cloudflare-ips.timer \
  deadlock-health-monitor.service \
  deadlock-health-monitor.timer; do
  install -m 0644 "$SYSTEMD_SRC_DIR/$unit_name" "$SYSTEMD_DEST_DIR/$unit_name"
done

"$ROOT_DIR/tools/platform_prepare_service_user.sh" \
  --app-dir "$APP_DIR" \
  --apply
systemctl daemon-reload

if [[ "$ENABLE_SYSTEMD_UNITS" == "1" ]]; then
  systemctl enable deadlock-api.service deadlock-worker.service deadlock-web.service
  systemctl enable --now \
    deadlock-maintenance.timer \
    deadlock-cloudflare-ips.timer \
    deadlock-health-monitor.timer
fi

cat <<EOF
Installed platform systemd units into:
  $SYSTEMD_DEST_DIR

Units:
  deadlock-api.service
  deadlock-worker.service
  deadlock-web.service
  deadlock-maintenance.service
  deadlock-maintenance.timer
  deadlock-offsite-backup.service
  deadlock-offsite-backup.timer
  deadlock-cloudflare-ips.service
  deadlock-cloudflare-ips.timer
  deadlock-health-monitor.service
  deadlock-health-monitor.timer

Prepared service-owned runtime paths under:
  $APP_DIR
EOF
