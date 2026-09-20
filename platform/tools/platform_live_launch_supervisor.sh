#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

# This supervisor is copied into the digest-bound live-QA generation.  It is
# never executed from a source checkout or active release tools directory.
# The fixed root entrypoint has already verified the generation and holds the
# canonical release lock for this complete provisioning/browser contour.
if (( $# != 4 )); then
  echo "Live browser supervisor received an invalid argument count" >&2
  exit 2
fi

base_url="$1"
provision="$2"
marker="$3"
target_sha="$4"
EXPECTED_ORIGIN="https://old-sparky.com"
INSTALL_ROOT="${PLATFORM_LIVE_QA_INSTALL_ROOT:-}"
TOOLS_DIR="$INSTALL_ROOT/platform/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_live_launch_supervisor.sh"
BUNDLE="/root/.oldsparky/liveqa/csp-live-qa.json"

test "$EUID" -eq 0 || {
  echo "Live browser supervisor must run as root" >&2
  exit 1
}
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] \
  || { echo "Live browser QA requires an exact target SHA" >&2; exit 1; }
[[ "$INSTALL_ROOT" =~ ^/root/\.oldsparky/liveqa/releases/[0-9a-f]{40}$ ]] \
  || { echo "Trusted live-QA install root is invalid" >&2; exit 1; }
[[ "$base_url" == "$EXPECTED_ORIGIN" ]] \
  || { echo "Unexpected production origin" >&2; exit 1; }
[[ "$INSTALL_ROOT" == "/root/.oldsparky/liveqa/releases/$target_sha" ]] \
  || { echo "Trusted live-QA install root does not match target SHA" >&2; exit 1; }
case "$provision" in
  true)
    [[ "$marker" =~ ^liveqa-[a-z0-9-]{6,56}$ ]] \
      || { echo "Provisioning requires a fresh liveqa marker" >&2; exit 2; }
    ;;
  false)
    [[ -z "$marker" ]] \
      || { echo "marker is allowed only with provision=true" >&2; exit 2; }
    ;;
  *)
    echo "provision must be true or false" >&2
    exit 2
    ;;
esac

for path in \
  "$INSTALL_ROOT" \
  "$INSTALL_ROOT/platform" \
  "$TOOLS_DIR" \
  "$SCRIPT_PATH" \
  "$TOOLS_DIR/platform_live_qa_guard.py" \
  "$TOOLS_DIR/platform_safe_env_exec.py" \
  "$TOOLS_DIR/platform_live_browser_qa.sh" \
  "$TOOLS_DIR/platform_provision_live_csp_qa.sh" \
  "$TOOLS_DIR/platform_install_live_qa_user.sh" \
  "$TOOLS_DIR/platform_release_lock_exec.sh" \
  "$TOOLS_DIR/platform_release_lock.sh"; do
  [[ -e "$path" && ! -L "$path" ]] \
    || { echo "Trusted live-QA generation member is unavailable" >&2; exit 1; }
done
[[ "$(/usr/bin/readlink -f -- "$SCRIPT_PATH")" == "$SCRIPT_PATH" ]] \
  || { echo "Trusted live-launch supervisor path is not canonical" >&2; exit 1; }
[[ "$(/usr/bin/stat -c '%u:%g:%h' -- "$SCRIPT_PATH")" == "0:0:1" ]] \
  || { echo "Trusted live-launch supervisor ownership is unsafe" >&2; exit 1; }

# The runtime verifier is the only operation that reads the active generation
# manifest here; it is root-installed in the same generation and its output is
# deliberately discarded.  This protects direct/replayed supervisor calls.
/usr/bin/python3.12 -I \
  /root/.oldsparky/liveqa/platform_live_user_qa_dispatch.py verify "$target_sha"

identity_report() {
  /usr/bin/python3.12 - <<'PY'
import grp
import json
import pwd

names = (
    "oldsparky",
    "oldsparky-platform",
    "oldsparky-api",
    "oldsparky-web",
    "oldsparky-worker",
    "oldsparky-liveqa",
)
users = {}
groups = {}
for name in names:
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        users[name] = None
    else:
        users[name] = {"uid": entry.pw_uid, "gid": entry.pw_gid, "home": entry.pw_dir, "shell": entry.pw_shell}
    try:
        entry = grp.getgrnam(name)
    except KeyError:
        groups[name] = None
    else:
        groups[name] = {"gid": entry.gr_gid}
failures = []
qa_user = users.get("oldsparky-liveqa")
qa_group = groups.get("oldsparky-liveqa")
if qa_user is None:
    failures.append("user_missing")
if qa_group is None:
    failures.append("group_missing")
required = ("oldsparky", "oldsparky-platform")
for name in required:
    if users.get(name) is None:
        failures.append(f"{name}_user_missing")
    if groups.get(name) is None:
        failures.append(f"{name}_group_missing")
if qa_user is not None and qa_group is not None:
    if qa_user["uid"] == 0 or any(
        name != "oldsparky-liveqa" and value is not None and value["uid"] == qa_user["uid"]
        for name, value in users.items()
    ):
        failures.append("uid_collision")
    if qa_user["gid"] == 0 or any(
        name != "oldsparky-liveqa" and value is not None and value["gid"] == qa_user["gid"]
        for name, value in users.items()
    ) or any(
        name != "oldsparky-liveqa" and value is not None and value["gid"] == qa_user["gid"]
        for name, value in groups.items()
    ):
        failures.append("gid_collision")
    if qa_user["gid"] != qa_group["gid"]:
        failures.append("user_group_gid_mismatch")
    if qa_user["home"] != "/nonexistent":
        failures.append("home_mismatch")
    if qa_user["shell"] != "/usr/sbin/nologin":
        failures.append("shell_mismatch")
    if [entry.pw_name for entry in pwd.getpwall() if entry.pw_uid == qa_user["uid"]] != ["oldsparky-liveqa"]:
        failures.append("uid_duplicate")
    if [entry.gr_name for entry in grp.getgrall() if entry.gr_gid == qa_user["gid"]] != ["oldsparky-liveqa"]:
        failures.append("gid_duplicate")
    if [entry.gr_name for entry in grp.getgrall() if entry.gr_gid != qa_user["gid"] and "oldsparky-liveqa" in entry.gr_mem]:
        failures.append("supplementary_groups")
print(
    "LIVE_QA_IDENTITY "
    + json.dumps(
        {
            "status": "invalid" if failures else "valid",
            "reasons": failures,
            "qa_user": qa_user,
            "qa_group": qa_group,
            "required_identities_present": all(
                users.get(name) is not None and groups.get(name) is not None for name in required
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
if failures:
    raise SystemExit(1)
PY
}

identity_report

if [[ "$provision" == "true" ]]; then
  # Account/profile installation is idempotent and is part of the launch
  # signal.  The profile is copied into the generation as a reviewed source.
  env -i \
    HOME=/root LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    PLATFORM_APP_DIR=/opt/oldsparky/platform \
    PLATFORM_LIVE_QA_INSTALL_ROOT="$INSTALL_ROOT" \
    PLATFORM_LIVE_QA_TARGET_SHA="$target_sha" \
    "$TOOLS_DIR/platform_install_live_qa_user.sh" --apply

  for path in \
    /opt/oldsparky \
    /opt/oldsparky/platform \
    /opt/oldsparky/platform/shared \
    /opt/oldsparky/platform/shared/.env.platform; do
    if [[ -e "$path" || -L "$path" ]]; then
      /usr/bin/stat -c 'LIVE_QA_ENV_PATH path=%n uid=%u gid=%g mode=%a type=%F' "$path"
    else
      echo "LIVE_QA_ENV_PATH path=$path status=missing"
    fi
  done

  env -i \
    HOME=/root LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    PLATFORM_APP_DIR=/opt/oldsparky/platform \
    PLATFORM_LIVE_QA_INSTALL_ROOT="$INSTALL_ROOT" \
    PLATFORM_LIVE_QA_TARGET_SHA="$target_sha" \
    "$TOOLS_DIR/platform_provision_live_csp_qa.sh" \
      --marker "$marker" \
      --bundle-path /root/.oldsparky/liveqa/csp-live-qa.json \
      --primary-email liveqa@auth.old-sparky.com \
      --mailbox-helper /root/.oldsparky/liveqa/platform_live_qa_mailbox_helper.py
fi

# Browser QA receives only the fixed public origin and installed generation
# identity. It reads the root-only bundle through the reviewed guard and does
# not receive credentials in its environment or command line.
env -i \
  HOME=/root LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  PLATFORM_APP_DIR=/opt/oldsparky/platform \
  PLATFORM_LIVE_CSP_QA_BUNDLE="$BUNDLE" \
  PLATFORM_LIVE_QA_INSTALL_ROOT="$INSTALL_ROOT" \
  PLATFORM_LIVE_QA_TARGET_SHA="$target_sha" \
  PLAYWRIGHT_LIVE_BASE_URL="$base_url" \
  "$TOOLS_DIR/platform_live_browser_qa.sh" public

echo "LIVE_BROWSER_QA_SUCCESS source_commit=$target_sha"
