#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${PLATFORM_APP_DIR:-/opt/oldsparky/platform}"
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

Dry-run by default. With --apply, creates isolated locked system identities for
web/API/worker, renders least-privilege runtime env files, and prepares only the
writable paths each service needs.
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

CANONICAL_ENV="$APP_DIR/shared/.env.platform"
RUNTIME_ENV_DIR="$APP_DIR/shared/env"
MEDIA_STAGING_DIR="$APP_DIR/shared/media-staging"
MEDIA_QUOTA_LOCK="$MEDIA_STAGING_DIR/.quota.lock"
WORKER_STATE_DIR="$APP_DIR/shared/worker-state"
WEB_CACHE_DIR="$APP_DIR/current/apps/platform_web/.next/cache"
MEDIA_GROUP="oldsparky-media"
SERVICE_USERS=(oldsparky-web oldsparky-api oldsparky-worker)

if [[ "$APPLY" -eq 0 ]]; then
  cat <<EOF
[DRY-RUN] Ensure private service identities: ${SERVICE_USERS[*]}
[DRY-RUN] Ensure exact supplementary groups: web=none, API/worker=$MEDIA_GROUP
[DRY-RUN] Validate unique system UID/GID range, nologin shell, /nonexistent home and locked passwords
[DRY-RUN] Lock canonical env to root:root mode 0600: $CANONICAL_ENV
[DRY-RUN] Render root-owned per-service envs under: $RUNTIME_ENV_DIR
[DRY-RUN] Prepare API/worker staging: $MEDIA_STAGING_DIR
[DRY-RUN] Prepare worker-only state: $WORKER_STATE_DIR
[DRY-RUN] Prepare web-only cache: $WEB_CACHE_DIR
EOF
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "--apply must run as root." >&2
  exit 1
fi
if [[ ! -d "$APP_DIR/shared" || ! -L "$APP_DIR/current" || ! -f "$CANONICAL_ENV" || -L "$CANONICAL_ENV" ]]; then
  echo "Platform release/shared layout is incomplete or unsafe under $APP_DIR." >&2
  exit 1
fi

login_defs_number() {
  local key="$1"
  local fallback="$2"
  local value
  value="$(awk -v key="$key" '$1 == key && $2 ~ /^[0-9]+$/ {print $2; exit}' /etc/login.defs)"
  printf '%s\n' "${value:-$fallback}"
}

SYS_UID_MIN="$(login_defs_number SYS_UID_MIN 100)"
SYS_UID_MAX="$(login_defs_number SYS_UID_MAX 999)"
SYS_GID_MIN="$(login_defs_number SYS_GID_MIN 100)"
SYS_GID_MAX="$(login_defs_number SYS_GID_MAX 999)"

validate_system_group() {
  local group_name="$1"
  local entry name _password gid members duplicate_count
  entry="$(getent group "$group_name")" || {
    echo "Required service group is missing: $group_name" >&2
    return 1
  }
  IFS=: read -r name _password gid members <<<"$entry"
  if [[ "$name" != "$group_name" || ! "$gid" =~ ^[0-9]+$ \
    || "$gid" -eq 0 || "$gid" -lt "$SYS_GID_MIN" || "$gid" -gt "$SYS_GID_MAX" ]]; then
    echo "$group_name must have a non-root system GID in $SYS_GID_MIN..$SYS_GID_MAX." >&2
    return 1
  fi
  duplicate_count="$(getent group | awk -F: -v gid="$gid" '$3 == gid {count++} END {print count + 0}')"
  if [[ "$duplicate_count" != "1" ]]; then
    echo "$group_name GID must be unique; found $duplicate_count group entries for $gid." >&2
    return 1
  fi
}

validate_service_user() {
  local service_user="$1"
  local entry name _password uid gid _gecos home shell primary_gid duplicate_count password_state
  entry="$(getent passwd "$service_user")" || {
    echo "Required service user is missing: $service_user" >&2
    return 1
  }
  IFS=: read -r name _password uid gid _gecos home shell <<<"$entry"
  primary_gid="$(getent group "$service_user" | cut -d: -f3)"
  if [[ "$name" != "$service_user" || ! "$uid" =~ ^[0-9]+$ \
    || "$uid" -eq 0 || "$uid" -lt "$SYS_UID_MIN" || "$uid" -gt "$SYS_UID_MAX" ]]; then
    echo "$service_user must have a non-root system UID in $SYS_UID_MIN..$SYS_UID_MAX." >&2
    return 1
  fi
  if [[ "$gid" != "$primary_gid" ]]; then
    echo "$service_user must use its private primary group." >&2
    return 1
  fi
  if [[ "$home" != "/nonexistent" || "$shell" != "/usr/sbin/nologin" ]]; then
    echo "$service_user must use /nonexistent and /usr/sbin/nologin." >&2
    return 1
  fi
  duplicate_count="$(getent passwd | awk -F: -v uid="$uid" '$3 == uid {count++} END {print count + 0}')"
  if [[ "$duplicate_count" != "1" ]]; then
    echo "$service_user UID must be unique; found $duplicate_count passwd entries for $uid." >&2
    return 1
  fi
  password_state="$(passwd -S "$service_user" | awk '{print $2}')"
  if [[ "$password_state" != "L" ]]; then
    echo "$service_user password must be locked." >&2
    return 1
  fi
}

clear_supplementary_groups() {
  local service_user="$1"
  local primary_group group_name
  primary_group="$(id -gn "$service_user")"
  for group_name in $(id -nG "$service_user"); do
    if [[ "$group_name" != "$primary_group" ]]; then
      gpasswd -d "$service_user" "$group_name" >/dev/null
    fi
  done
}

for service_user in "${SERVICE_USERS[@]}"; do
  if ! getent group "$service_user" >/dev/null; then
    groupadd --system "$service_user"
  fi
  if ! id "$service_user" >/dev/null 2>&1; then
    useradd --system --gid "$service_user" --home-dir /nonexistent \
      --shell /usr/sbin/nologin --no-create-home "$service_user"
  fi
  usermod --lock --home /nonexistent --shell /usr/sbin/nologin "$service_user"
  validate_system_group "$service_user"
  validate_service_user "$service_user"
  clear_supplementary_groups "$service_user"
done

if ! getent group "$MEDIA_GROUP" >/dev/null; then
  groupadd --system "$MEDIA_GROUP"
fi
validate_system_group "$MEDIA_GROUP"
usermod -a -G "$MEDIA_GROUP" oldsparky-api
usermod -a -G "$MEDIA_GROUP" oldsparky-worker

for service_user in "${SERVICE_USERS[@]}"; do
  primary_group="$(id -gn "$service_user")"
  supplementary="$(
    id -nG "$service_user" \
      | tr ' ' '\n' \
      | grep -vxF "$primary_group" \
      | sort \
      | paste -sd, - \
      || true
  )"
  expected=""
  if [[ "$service_user" == "oldsparky-api" || "$service_user" == "oldsparky-worker" ]]; then
    expected="$MEDIA_GROUP"
  fi
  if [[ "$supplementary" != "$expected" ]]; then
    echo "$service_user supplementary groups are unsafe: ${supplementary:-none}; expected ${expected:-none}." >&2
    exit 1
  fi
done

# The canonical operator env is the one source of truth, but no runtime identity
# may read it directly.
chown root:root "$CANONICAL_ENV"
chmod 0600 "$CANONICAL_ENV"

install -d -o root -g root -m 0711 "$RUNTIME_ENV_DIR"
install -d -o root -g "$MEDIA_GROUP" -m 2770 "$MEDIA_STAGING_DIR"
install -d -o oldsparky-worker -g oldsparky-worker -m 0700 "$WORKER_STATE_DIR"
install -d -o oldsparky-web -g oldsparky-web -m 0750 "$WEB_CACHE_DIR"

# Migrate state created by the former shared service identity. Symlinks are
# rejected before recursive ownership changes so an unexpected path cannot
# escape these dedicated runtime directories.
for mutable_dir in "$MEDIA_STAGING_DIR" "$WORKER_STATE_DIR" "$WEB_CACHE_DIR"; do
  if find "$mutable_dir" -xdev -type l -print -quit | grep -q .; then
    echo "Refusing symlink inside mutable runtime directory: $mutable_dir" >&2
    exit 1
  fi
done
if [[ -L "$MEDIA_QUOTA_LOCK" || ( -e "$MEDIA_QUOTA_LOCK" && ! -f "$MEDIA_QUOTA_LOCK" ) ]]; then
  echo "Refusing unsafe media quota lock path: $MEDIA_QUOTA_LOCK" >&2
  exit 1
fi
chown -R root:"$MEDIA_GROUP" "$MEDIA_STAGING_DIR"
find "$MEDIA_STAGING_DIR" -xdev -type d -exec chmod 2770 {} +
find "$MEDIA_STAGING_DIR" -xdev -type f -exec chmod 0660 {} +
if [[ ! -e "$MEDIA_QUOTA_LOCK" ]]; then
  install -o root -g "$MEDIA_GROUP" -m 0660 /dev/null "$MEDIA_QUOTA_LOCK"
else
  chown root:"$MEDIA_GROUP" "$MEDIA_QUOTA_LOCK"
  chmod 0660 "$MEDIA_QUOTA_LOCK"
fi
chown -R oldsparky-worker:oldsparky-worker "$WORKER_STATE_DIR"
find "$WORKER_STATE_DIR" -xdev -type d -exec chmod 0700 {} +
find "$WORKER_STATE_DIR" -xdev -type f -exec chmod 0600 {} +
chown -R oldsparky-web:oldsparky-web "$WEB_CACHE_DIR"
find "$WEB_CACHE_DIR" -xdev -type d -exec chmod 0750 {} +
find "$WEB_CACHE_DIR" -xdev -type f -exec chmod 0640 {} +

python3 "$APP_DIR/current/tools/platform_render_service_envs.py" \
  --source "$CANONICAL_ENV" \
  --output-dir "$RUNTIME_ENV_DIR" \
  --apply

if [[ "$(stat -c '%U:%G:%a' "$CANONICAL_ENV")" != "root:root:600" ]]; then
  echo "Canonical env ownership verification failed." >&2
  exit 1
fi

declare -A ENV_PATHS=(
  [oldsparky-web]="$RUNTIME_ENV_DIR/web.env"
  [oldsparky-api]="$RUNTIME_ENV_DIR/api.env"
  [oldsparky-worker]="$RUNTIME_ENV_DIR/worker.env"
)
for service_user in "${SERVICE_USERS[@]}"; do
  own_env="${ENV_PATHS[$service_user]}"
  if ! runuser -u "$service_user" -- test -r "$own_env"; then
    echo "$service_user cannot read its runtime env." >&2
    exit 1
  fi
  if runuser -u "$service_user" -- test -r "$CANONICAL_ENV"; then
    echo "$service_user must not be able to read the canonical env." >&2
    exit 1
  fi
  for other_user in "${SERVICE_USERS[@]}"; do
    if [[ "$other_user" == "$service_user" ]]; then
      continue
    fi
    if runuser -u "$service_user" -- test -r "${ENV_PATHS[$other_user]}"; then
      echo "$service_user must not read ${other_user}'s runtime env." >&2
      exit 1
    fi
  done
done

for service_user in oldsparky-api oldsparky-worker; do
  if ! runuser -u "$service_user" -- test -x "$APP_DIR/shared/venv/bin/python"; then
    echo "$service_user cannot execute the shared Python runtime." >&2
    exit 1
  fi
  if ! runuser -u "$service_user" -- test -w "$MEDIA_STAGING_DIR"; then
    echo "$service_user cannot write media staging." >&2
    exit 1
  fi
done
if runuser -u oldsparky-web -- test -w "$MEDIA_STAGING_DIR"; then
  echo "Web runtime must not write media staging." >&2
  exit 1
fi
if ! runuser -u oldsparky-worker -- test -w "$WORKER_STATE_DIR"; then
  echo "Worker cannot write its state directory." >&2
  exit 1
fi
if runuser -u oldsparky-api -- test -w "$WORKER_STATE_DIR" \
  || runuser -u oldsparky-web -- test -w "$WORKER_STATE_DIR"; then
  echo "Only worker may write worker state." >&2
  exit 1
fi
if ! runuser -u oldsparky-web -- test -w "$WEB_CACHE_DIR"; then
  echo "Web cannot write its cache directory." >&2
  exit 1
fi

cat <<EOF
[OK] Runtime identities, env boundaries and writable paths are isolated.
Canonical secrets remain root-only; service envs were regenerated from that source.
EOF
