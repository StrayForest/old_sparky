#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1

# This supervisor is copied into the digest-bound live-QA generation.  It is
# never executed from a source checkout or active release tools directory.
# The fixed root entrypoint has already verified the generation and holds the
# canonical release lock for this complete provisioning/browser contour.
if (( $# != 4 && $# != 6 )); then
  echo "Live browser supervisor received an invalid argument count" >&2
  exit 2
fi

base_url="$1"
provision="$2"
marker="$3"
runner_sha="$4"
source_arguments=()
if (( $# == 6 )); then
  [[ "$5" == "--source-binding-base64" ]] \
    || { echo "Live browser source-binding option is invalid" >&2; exit 2; }
  source_arguments=("$5" "$6")
fi
app_target_sha="${PLATFORM_LIVE_QA_TARGET_SHA:-$runner_sha}"
launch_stage="validation"
launch_check="root_uid"
# Keep the remote protocol on a dedicated descriptor.  Every ordinary
# diagnostic and child output is discarded; the dispatcher accepts only this
# one fixed, source-bound line.
exec 3>&1
exec >/dev/null 2>&1
emit_launch_status() {
  local exit_code="$1"
  local status="failed"
  local stage="$launch_stage"
  local check="$launch_check"
  if [[ "$exit_code" == "0" ]]; then
    status="passed"
    stage="complete"
    check="none"
  fi
  if [[ "$runner_sha" =~ ^[0-9a-f]{40}$ ]] \
    && [[ "$app_target_sha" =~ ^[0-9a-f]{40}$ ]] \
    && [[ "$exit_code" =~ ^[0-9]{1,3}$ ]] \
    && (( exit_code <= 255 )); then
    case "$check" in
      none|root_uid|source_identity|install_root_format|origin|install_root_target|\
      source_binding|provision_mode|provision_marker|marker_absent|marker_digest|\
      generation_members|generation_supervisor_path|generation_supervisor_metadata|\
      generation_manifest|identity|account_install|provision|browser_qa) ;;
      *) check="none" ;;
    esac
    printf 'LIVE_LAUNCH_STATUS schema=2 status=%s stage=%s check=%s child_exit=%s source_sha=%s\n' \
      "$status" "$stage" "$check" "$exit_code" "$runner_sha" >&3 || true
  fi
}
trap 'launch_exit=$?; trap - EXIT; emit_launch_status "$launch_exit"' EXIT

EXPECTED_ORIGIN="https://old-sparky.com"
INSTALL_ROOT="${PLATFORM_LIVE_QA_INSTALL_ROOT:-}"
TOOLS_DIR="$INSTALL_ROOT/platform/tools"
SCRIPT_PATH="$TOOLS_DIR/platform_live_launch_supervisor.sh"
BUNDLE="/root/.oldsparky/liveqa/csp-live-qa.json"

launch_check="root_uid"
test "$EUID" -eq 0 || {
  echo "Live browser supervisor must run as root" >&2
  exit 1
}
launch_check="source_identity"
[[ "$runner_sha" =~ ^[0-9a-f]{40}$ && "$app_target_sha" =~ ^[0-9a-f]{40}$ ]] \
  || { echo "Live browser QA requires an exact target SHA" >&2; exit 1; }
launch_check="install_root_format"
[[ "$INSTALL_ROOT" =~ ^/root/\.oldsparky/liveqa/releases/[0-9a-f]{40}$ ]] \
  || { echo "Trusted live-QA install root is invalid" >&2; exit 1; }
launch_check="origin"
[[ "$base_url" == "$EXPECTED_ORIGIN" ]] \
  || { echo "Unexpected production origin" >&2; exit 1; }
launch_check="install_root_target"
[[ "$INSTALL_ROOT" == "/root/.oldsparky/liveqa/releases/$app_target_sha" ]] \
  || { echo "Trusted live-QA install root does not match target SHA" >&2; exit 1; }
if (( ${#source_arguments[@]} )); then
  launch_check="source_binding"
  [[ "${PLATFORM_LIVE_QA_SOURCE_BINDING_BASE64:-}" == "${source_arguments[1]}" ]] \
    || { echo "Live browser source-binding handoff changed" >&2; exit 1; }
else
  launch_check="source_binding"
  [[ "$app_target_sha" == "$runner_sha" \
    && -z "${PLATFORM_LIVE_QA_SOURCE_BINDING_BASE64:-}" ]] \
    || { echo "Same-source live browser identities differ" >&2; exit 1; }
fi
launch_check="provision_mode"
case "$provision" in
  true)
    launch_check="provision_marker"
    [[ "$marker" =~ ^liveqa-[a-z0-9-]{6,56}$ ]] \
      || { echo "Provisioning requires a fresh liveqa marker" >&2; exit 2; }
    ;;
  false)
    launch_check="marker_absent"
    [[ -z "$marker" ]] \
      || { echo "marker is allowed only with provision=true" >&2; exit 2; }
    ;;
  *)
    echo "provision must be true or false" >&2
    exit 2
    ;;
esac
launch_check="marker_digest"
marker_sha256="$(printf '%s' "$marker" | /usr/bin/sha256sum)"
marker_sha256="${marker_sha256%% *}"
[[ "$marker_sha256" =~ ^[0-9a-f]{64}$ ]] \
  || { echo "Live browser marker binding is invalid" >&2; exit 2; }

launch_stage="trusted_generation"
launch_check="generation_members"
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
launch_check="generation_supervisor_path"
[[ "$(/usr/bin/readlink -f -- "$SCRIPT_PATH")" == "$SCRIPT_PATH" ]] \
  || { echo "Trusted live-launch supervisor path is not canonical" >&2; exit 1; }
launch_check="generation_supervisor_metadata"
[[ "$(/usr/bin/stat -c '%u:%g:%h' -- "$SCRIPT_PATH")" == "0:0:1" ]] \
  || { echo "Trusted live-launch supervisor ownership is unsafe" >&2; exit 1; }

# The runtime verifier is the only operation that reads the active generation
# manifest here; it is root-installed in the same generation and its output is
# deliberately discarded.  This protects direct/replayed supervisor calls.
launch_check="generation_manifest"
/usr/bin/python3.12 -I -B \
  /root/.oldsparky/liveqa/platform_live_user_qa_dispatch.py verify \
  "$runner_sha" "${source_arguments[@]}"

identity_report() {
  /usr/bin/python3.12 -I -B - <<'PY'
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
required = ("oldsparky-platform",)
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

launch_stage="identity"
launch_check="identity"
identity_report

if [[ "$provision" == "true" ]]; then
  launch_stage="account_install"
  launch_check="account_install"
  # Account/profile installation is idempotent and is part of the launch
  # signal.  The profile is copied into the generation as a reviewed source.
  env -i \
    HOME=/root LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    PLATFORM_APP_DIR=/opt/oldsparky/platform \
    PLATFORM_LIVE_QA_INSTALL_ROOT="$INSTALL_ROOT" \
    PLATFORM_LIVE_QA_TARGET_SHA="$app_target_sha" \
    PLATFORM_LIVE_QA_RUNNER_SHA="$runner_sha" \
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

  launch_stage="provision"
  launch_check="provision"
  env -i \
    HOME=/root LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    PLATFORM_APP_DIR=/opt/oldsparky/platform \
    PLATFORM_LIVE_QA_INSTALL_ROOT="$INSTALL_ROOT" \
    PLATFORM_LIVE_QA_TARGET_SHA="$app_target_sha" \
    PLATFORM_LIVE_QA_RUNNER_SHA="$runner_sha" \
    "$TOOLS_DIR/platform_provision_live_csp_qa.sh" \
      --marker "$marker" \
      --bundle-path /root/.oldsparky/liveqa/csp-live-qa.json \
      --primary-email liveqa@auth.old-sparky.com \
      --mailbox-helper /root/.oldsparky/liveqa/platform_live_qa_mailbox_helper.py
fi

# Browser QA receives only the fixed public origin and installed generation
# identity. It reads the root-only bundle through the reviewed guard and does
# not receive credentials in its environment or command line.
launch_stage="browser_qa"
launch_check="browser_qa"
browser_diagnostic=""
if browser_diagnostic="$(env -i \
  HOME=/root LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  PLATFORM_APP_DIR=/opt/oldsparky/platform \
  PLATFORM_LIVE_CSP_QA_BUNDLE="$BUNDLE" \
  PLATFORM_LIVE_QA_INSTALL_ROOT="$INSTALL_ROOT" \
  PLATFORM_LIVE_QA_TARGET_SHA="$app_target_sha" \
  PLATFORM_LIVE_QA_RUNNER_SHA="$runner_sha" \
  PLATFORM_LIVE_QA_MARKER_SHA256="$marker_sha256" \
  PLAYWRIGHT_LIVE_BASE_URL="$base_url" \
  "$TOOLS_DIR/platform_live_browser_qa.sh" public)"; then
  browser_status=0
else
  browser_status=$?
fi
if [[ "$browser_diagnostic" =~ (^|$'\n')LIVE_QA_CHILD_DIAGNOSTIC\ schema=1\ kind=(none|playwright_cli_usage|node_module_missing|browser_executable_missing|browser_launch_error|child_timeout|cleanup_failure|unclassified)\ stdout_bytes=[0-9]{1,16}\ stderr_bytes=[0-9]{1,16}\ truncated=(true|false)\ child_exit=[0-9]{1,3}($|$'\n') ]]; then
  child_diagnostic="${BASH_REMATCH[0]}"
  child_diagnostic="${child_diagnostic//$'\n'/}"
  printf '%s\n' "$child_diagnostic" >&3 || true
else
  browser_status=2
fi
if (( browser_status != 0 )); then
  exit "$browser_status"
fi
