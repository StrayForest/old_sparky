#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_SRC_DIR="$ROOT_DIR/deploy/systemd"
SYSTEMD_DEST_DIR="${PLATFORM_SYSTEMD_DIR:-/etc/systemd/system}"
APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
ENABLE_SYSTEMD_UNITS="${PLATFORM_ENABLE_SYSTEMD_UNITS:-1}"

CURRENT_UNITS=(
  deadlock-api.service
  deadlock-worker.service
  deadlock-web.service
  deadlock-maintenance.service
  deadlock-maintenance.timer
  deadlock-logrotate.service
  deadlock-logrotate.timer
  deadlock-offsite-backup.service
  deadlock-offsite-backup.timer
  deadlock-cloudflare-ips.service
  deadlock-cloudflare-ips.timer
  deadlock-health-monitor.service
  deadlock-health-monitor.timer
)

declare -A EXPECTED_UNIT=()
for unit_name in "${CURRENT_UNITS[@]}"; do
  EXPECTED_UNIT["$unit_name"]=1
done

RETIRED_UNITS=()
if [[ "$SYSTEMD_DEST_DIR" == "/etc/systemd/system" && "$ENABLE_SYSTEMD_UNITS" == "1" ]]; then
  shopt -s nullglob
  for unit_path in "$SYSTEMD_DEST_DIR"/deadlock-*.service "$SYSTEMD_DEST_DIR"/deadlock-*.timer; do
    unit_name="$(basename "$unit_path")"
    if [[ -n "${EXPECTED_UNIT[$unit_name]:-}" ]]; then
      continue
    fi
    if systemctl is-active --quiet "$unit_name"; then
      systemctl stop "$unit_name"
    fi
    if systemctl is-enabled --quiet "$unit_name"; then
      systemctl disable "$unit_name"
    fi
    rm -f -- "$unit_path"
    RETIRED_UNITS+=("$unit_name")
  done
  shopt -u nullglob
fi

for unit_name in "${CURRENT_UNITS[@]}"; do
  install -o root -g root -m 0644 "$SYSTEMD_SRC_DIR/$unit_name" "$SYSTEMD_DEST_DIR/$unit_name"
done

if [[ "$SYSTEMD_DEST_DIR" == "/etc/systemd/system" ]]; then
  "$ROOT_DIR/tools/platform_install_logging.sh"
fi

"$ROOT_DIR/tools/platform_prepare_service_user.sh" \
  --app-dir "$APP_DIR" \
  --apply
systemctl daemon-reload

if [[ "$ENABLE_SYSTEMD_UNITS" == "1" ]]; then
  systemctl enable deadlock-api.service deadlock-worker.service deadlock-web.service
  systemctl enable --now \
    deadlock-maintenance.timer \
    deadlock-logrotate.timer \
    deadlock-cloudflare-ips.timer \
    deadlock-health-monitor.timer
fi

cat <<EOF
Installed platform systemd units into:
  $SYSTEMD_DEST_DIR

Units:
$(printf '  %s\n' "${CURRENT_UNITS[@]}")
Prepared service-owned runtime paths under:
  $APP_DIR
EOF

if (( ${#RETIRED_UNITS[@]} > 0 )); then
  printf 'Removed retired managed units:\n'
  printf '  %s\n' "${RETIRED_UNITS[@]}"
fi

# The public SSH host-key fingerprint is intentionally emitted during real
# production activation so CI can pin the already-trusted server in the next
# release. This never reads or exposes the private host key.
if [[ "$SYSTEMD_DEST_DIR" == "/etc/systemd/system" \
  && -x /usr/bin/ssh-keygen \
  && -f /etc/ssh/ssh_host_ed25519_key.pub \
  && ! -L /etc/ssh/ssh_host_ed25519_key.pub ]]; then
  host_key_meta="$(stat -c '%u:%g:%a:%h' /etc/ssh/ssh_host_ed25519_key.pub)"
  if [[ "$host_key_meta" == "0:0:644:1" || "$host_key_meta" == "0:0:640:1" ]]; then
    host_key_fingerprint="$(
      /usr/bin/ssh-keygen -E sha256 -lf /etc/ssh/ssh_host_ed25519_key.pub \
        | /usr/bin/awk '{print $2}'
    )"
    if [[ "$host_key_fingerprint" == SHA256:* ]]; then
      printf 'PRODUCTION_SSH_ED25519_FINGERPRINT=%s\n' "$host_key_fingerprint"
    fi
  fi
fi
