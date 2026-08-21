#!/usr/bin/env bash
set +x
set -euo pipefail

ACCOUNT_NAME="oldsparky-liveqa"
APPLY=0
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
APPARMOR_PROFILE_NAME="oldsparky-liveqa-chromium"
APPARMOR_PROFILE_SOURCE="$ROOT_DIR/deploy/apparmor/$APPARMOR_PROFILE_NAME"
APPARMOR_PROFILE_TARGET="/etc/apparmor.d/$APPARMOR_PROFILE_NAME"
APPARMOR_PROFILES="/sys/kernel/security/apparmor/profiles"

if (( $# == 1 )) && [[ "$1" == "--apply" ]]; then
  APPLY=1
elif (( $# != 0 )); then
  echo "Usage: $0 [--apply]" >&2
  exit 2
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "The dedicated live QA account installer requires root." >&2
  exit 1
fi

validate_existing() {
  /usr/bin/python3.12 -I - "$ACCOUNT_NAME" <<'PY'
import grp
import pwd
import sys

name = sys.argv[1]
production_names = {
    "oldsparky",
    "oldsparky-platform",
    "oldsparky-api",
    "oldsparky-web",
    "oldsparky-worker",
}
try:
    account = pwd.getpwnam(name)
    primary = grp.getgrnam(name)
except KeyError:
    raise SystemExit(1)
production_accounts = []
production_groups = []
for production_name in sorted(production_names):
    try:
        production_accounts.append(pwd.getpwnam(production_name))
    except KeyError:
        if production_name in {"oldsparky", "oldsparky-platform"}:
            raise SystemExit(1)
    try:
        production_groups.append(grp.getgrnam(production_name))
    except KeyError:
        if production_name in {"oldsparky", "oldsparky-platform"}:
            raise SystemExit(1)
passwd_matches = [entry.pw_name for entry in pwd.getpwall() if entry.pw_uid == account.pw_uid]
group_matches = [entry.gr_name for entry in grp.getgrall() if entry.gr_gid == account.pw_gid]
supplementary = [
    group.gr_name
    for group in grp.getgrall()
    if group.gr_gid != account.pw_gid and name in group.gr_mem
]
forbidden_uids = {0, *(entry.pw_uid for entry in production_accounts)}
forbidden_gids = {
    0,
    *(entry.pw_gid for entry in production_accounts),
    *(entry.gr_gid for entry in production_groups),
}
if (
    account.pw_uid in forbidden_uids
    or account.pw_gid in forbidden_gids
    or account.pw_gid != primary.gr_gid
    or passwd_matches != [name]
    or group_matches != [name]
    or account.pw_dir != "/nonexistent"
    or account.pw_shell != "/usr/sbin/nologin"
    or name in primary.gr_mem
    or supplementary
):
    raise SystemExit(2)
PY
}

ACCOUNT_READY=0
if validate_existing; then
  ACCOUNT_READY=1
elif /usr/bin/getent passwd "$ACCOUNT_NAME" >/dev/null \
  || /usr/bin/getent group "$ACCOUNT_NAME" >/dev/null; then
  echo "Existing oldsparky-liveqa user/group does not match the exact security contract." >&2
  exit 1
fi

validate_profile_source() {
  if [[ ! -f "$APPARMOR_PROFILE_SOURCE" || -L "$APPARMOR_PROFILE_SOURCE" \
    || "$(stat -c %u:%h "$APPARMOR_PROFILE_SOURCE")" != "0:1" ]]; then
    echo "Reviewed live QA AppArmor profile source is unsafe." >&2
    return 1
  fi
  local source_mode
  source_mode="$(stat -c %a "$APPARMOR_PROFILE_SOURCE")"
  if (( 8#$source_mode & 8#022 )); then
    echo "Reviewed live QA AppArmor profile source is writable outside root." >&2
    return 1
  fi
  /usr/sbin/apparmor_parser -Q -T "$APPARMOR_PROFILE_SOURCE" >/dev/null
}

profile_is_active() {
  [[ -f "$APPARMOR_PROFILE_TARGET" && ! -L "$APPARMOR_PROFILE_TARGET" \
    && "$(stat -c %U:%G:%a:%h "$APPARMOR_PROFILE_TARGET")" == "root:root:644:1" ]] \
    && /usr/bin/cmp -s "$APPARMOR_PROFILE_SOURCE" "$APPARMOR_PROFILE_TARGET" \
    && /usr/bin/grep -Fqx 'oldsparky-liveqa-chromium (unconfined)' "$APPARMOR_PROFILES" \
    && /usr/bin/grep -Fqx 'oldsparky-liveqa-chromium-headless (unconfined)' "$APPARMOR_PROFILES"
}

validate_profile_source
if (( APPLY == 0 )); then
  if (( ACCOUNT_READY == 1 )); then
    echo "Dedicated oldsparky-liveqa account contract is valid."
  else
    echo "Dry-run: dedicated oldsparky-liveqa system user/group must be created."
  fi
  if profile_is_active; then
    echo "Dedicated oldsparky-liveqa Chromium AppArmor profile is active."
  else
    echo "Dry-run: dedicated oldsparky-liveqa Chromium AppArmor profile must be installed."
  fi
  exit 0
fi

CREATED_GROUP=0
CREATED_USER=0
rollback_partial_identity() {
  local status=$?
  trap - EXIT INT TERM HUP
  if (( status != 0 )); then
    if (( CREATED_USER == 1 )) \
      && /usr/bin/getent passwd "$ACCOUNT_NAME" >/dev/null; then
      /usr/sbin/userdel "$ACCOUNT_NAME" >/dev/null 2>&1 || true
    fi
    if (( CREATED_GROUP == 1 )) \
      && /usr/bin/getent group "$ACCOUNT_NAME" >/dev/null; then
      /usr/sbin/groupdel "$ACCOUNT_NAME" >/dev/null 2>&1 || true
    fi
  fi
  exit "$status"
}
trap rollback_partial_identity EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if (( ACCOUNT_READY == 0 )); then
  /usr/sbin/groupadd --system "$ACCOUNT_NAME"
  CREATED_GROUP=1
  /usr/sbin/useradd \
    --system \
    --gid "$ACCOUNT_NAME" \
    --no-user-group \
    --home-dir /nonexistent \
    --shell /usr/sbin/nologin \
    --no-create-home \
    "$ACCOUNT_NAME"
  CREATED_USER=1
fi

if ! validate_existing; then
  echo "Created account failed its exact security contract." >&2
  exit 1
fi
/usr/bin/install -o root -g root -m 0644 \
  "$APPARMOR_PROFILE_SOURCE" "$APPARMOR_PROFILE_TARGET"
/usr/sbin/apparmor_parser -r -T "$APPARMOR_PROFILE_TARGET"
if ! profile_is_active; then
  echo "Installed live QA Chromium AppArmor profile failed validation." >&2
  exit 1
fi
trap - EXIT INT TERM HUP
echo "Dedicated oldsparky-liveqa identity and Chromium AppArmor profile are valid."
