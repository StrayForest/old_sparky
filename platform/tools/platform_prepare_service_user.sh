#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
SERVICE_USER="oldsparky-platform"
SERVICE_GROUP="oldsparky-platform"
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      APP_DIR="$2"
      shift 2
      ;;
    --apply)
      APPLY=1
      shift
      ;;
    --help|-h)
      cat <<'EOF'
Usage: platform_prepare_service_user.sh [--app-dir PATH] [--apply]

Dry-run by default. With --apply, creates the locked system account used by
the API/web/worker units and prepares only their required writable paths.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$APP_DIR" == "/" || -z "$APP_DIR" ]]; then
  echo "Refusing an unsafe app directory." >&2
  exit 1
fi

ENV_FILE="$APP_DIR/shared/.env.platform"
MEDIA_STAGING_DIR="$APP_DIR/shared/media-staging"
WORKER_STATE_DIR="$APP_DIR/shared/worker-state"
WEB_CACHE_DIR="$APP_DIR/current/apps/platform_web/.next/cache"

if [[ "$APPLY" -eq 0 ]]; then
  cat <<EOF
[DRY-RUN] Ensure locked system user/group: $SERVICE_USER
[DRY-RUN] Set $ENV_FILE to root:$SERVICE_GROUP mode 0640
[DRY-RUN] Prepare writable media staging: $MEDIA_STAGING_DIR
[DRY-RUN] Prepare writable worker state: $WORKER_STATE_DIR
[DRY-RUN] Prepare writable current web cache: $WEB_CACHE_DIR
EOF
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "--apply must run as root." >&2
  exit 1
fi
if [[ ! -d "$APP_DIR/shared" || ! -L "$APP_DIR/current" || ! -f "$ENV_FILE" ]]; then
  echo "Platform release/shared layout is incomplete under $APP_DIR." >&2
  exit 1
fi

if ! getent group "$SERVICE_GROUP" >/dev/null; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_GROUP" --home-dir /nonexistent \
    --shell /usr/sbin/nologin --no-create-home "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$MEDIA_STAGING_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$WORKER_STATE_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$WEB_CACHE_DIR"
chown root:"$SERVICE_GROUP" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

if [[ "$(stat -c '%U:%G:%a' "$ENV_FILE")" != "root:$SERVICE_GROUP:640" ]]; then
  echo "Shared env ownership verification failed." >&2
  exit 1
fi
for writable_dir in "$MEDIA_STAGING_DIR" "$WORKER_STATE_DIR" "$WEB_CACHE_DIR"; do
  if [[ "$(stat -c '%U:%G' "$writable_dir")" != "$SERVICE_USER:$SERVICE_GROUP" ]]; then
    echo "Service writable-directory ownership verification failed: $writable_dir" >&2
    exit 1
  fi
  if ! runuser -u "$SERVICE_USER" -- test -w "$writable_dir"; then
    echo "Service user cannot write required directory: $writable_dir" >&2
    exit 1
  fi
done
if ! runuser -u "$SERVICE_USER" -- test -r "$ENV_FILE"; then
  echo "Service user cannot read the shared env file." >&2
  exit 1
fi
if runuser -u "$SERVICE_USER" -- test -w "$ENV_FILE"; then
  echo "Service user must not be able to write the shared env file." >&2
  exit 1
fi
if ! runuser -u "$SERVICE_USER" -- test -x "$APP_DIR/shared/venv/bin/python"; then
  echo "Service user cannot execute the shared Python runtime." >&2
  exit 1
fi

cat <<EOF
[OK] Prepared $SERVICE_USER without changing or restarting running services.
Next: install the reviewed units, daemon-reload, restart one service at a time,
and verify rollback before continuing.
EOF
