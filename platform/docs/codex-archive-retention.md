# Codex archived-session retention

This procedure owns the local Codex archive-retention companion. It deletes only
sessions whose state row, archived timestamp, and canonical archive identity
agree. Codex's native archived-only delete rechecks lifecycle and history
references under writer locks. The wrapper reads metadata only (IDs, flags and
timestamps, paths, spawn edges, names and file stats); it never opens rollout
contents or directly deletes rows or files.

The [source lock](../tools/codex_archive_retention/codex-source.lock.json)
binds the upstream pin and reviewed patch. The patch aligns 170 local
`codex-rs/Cargo.lock` package versions with source manifests (`0.166.0`),
without changing third-party records or dependencies; all native commands use
`--locked`. A private companion build runs on same-repository PRs to `dev`,
checks out exact head and tested-merge SHAs, applies the context-free patch
strictly, runs focused native checks, and publishes seven-day evidence. It has
read-only repository permission and never installs, enables a timer, reads a
Codex home, or deletes sessions.

## Build artifact and activation

Build evidence is review-only. Never install an unreviewed PR. Independently
review the platform/upstream commits, patch digest, native test results,
artifact manifest, wrapper, and systemd boundaries. The manifest binds source
SHA, upstream tag/commit, patch, toolchain, target, CLI version, and binary
size/digest. Require successful exact-attempt native and canonical `Platform
security and build` runs for the same PR head and tested merge. The Security
route must be `full`, `fallback=false`, and `deployable=false`. Match PR,
repository, base, head, merge, upstream commit, patch, run, and attempt. Fetch
raw ZIPs and API metadata; `gh run download` alone does not verify GitHub's
artifact digest.

```bash
set -euo pipefail
REPO=StrayForest/old_sparky
PR='<same-repository PR number>'
HEAD_SHA='<exact PR head SHA>'
MERGE_SHA='<exact merge SHA tested by Security>'
BUILD_RUN_ID='<successful native build run ID>'
BUILD_ATTEMPT='<native build run attempt>'
SECURITY_RUN_ID='<successful Platform security and build run ID>'
SECURITY_ATTEMPT='<Security run attempt>'
ARTIFACT_DIR="$(mktemp -d /dev/shm/codex-archive-retention.XXXXXX)"
chmod 700 "$ARTIFACT_DIR"
test "$(id -u)" -eq 0
test "$(stat -c '%u:%a' "$ARTIFACT_DIR")" = '0:700'
BUILD_NAME="codex-archive-retention-$BUILD_RUN_ID-$BUILD_ATTEMPT"
SECURITY_ROUTE_NAME="platform-ci-route-$SECURITY_RUN_ID-$SECURITY_ATTEMPT"
gh api "repos/$REPO/actions/runs/$BUILD_RUN_ID/attempts/$BUILD_ATTEMPT" > "$ARTIFACT_DIR/build-run.json"
gh api "repos/$REPO/actions/runs/$SECURITY_RUN_ID/attempts/$SECURITY_ATTEMPT" > "$ARTIFACT_DIR/security-run.json"
gh api "repos/$REPO/actions/artifacts?name=$BUILD_NAME&per_page=100" > "$ARTIFACT_DIR/build-artifacts.json"
gh api "repos/$REPO/actions/artifacts?name=$SECURITY_ROUTE_NAME&per_page=100" > "$ARTIFACT_DIR/security-artifacts.json"
BUILD_ARTIFACT_ID="$(python3 -I -B - "$ARTIFACT_DIR/build-artifacts.json" "$BUILD_NAME" "$BUILD_RUN_ID" <<'PY'
import json
from pathlib import Path
import re
import sys
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rows = payload.get("artifacts") if isinstance(payload, dict) else None
if payload.get("total_count") != 1 or not isinstance(rows, list) or len(rows) != 1:
    raise SystemExit("build artifact metadata is not singular")
row = rows[0]
workflow = row.get("workflow_run") if isinstance(row, dict) else None
if (row.get("name") != sys.argv[2] or row.get("expired") is not False
    or not isinstance(workflow, dict) or str(workflow.get("id")) != sys.argv[3]
    or type(row.get("id")) is not int or row["id"] <= 0
    or re.fullmatch(r"sha256:[0-9a-f]{64}", row.get("digest", "")) is None):
    raise SystemExit("build artifact does not bind to the requested run")
print(row["id"])
PY
)"
SECURITY_ARTIFACT_ID="$(python3 -I -B - "$ARTIFACT_DIR/security-artifacts.json" "$SECURITY_ROUTE_NAME" "$SECURITY_RUN_ID" <<'PY'
import json
from pathlib import Path
import re
import sys
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rows = payload.get("artifacts") if isinstance(payload, dict) else None
if payload.get("total_count") != 1 or not isinstance(rows, list) or len(rows) != 1:
    raise SystemExit("Security route artifact metadata is not singular")
row = rows[0]
workflow = row.get("workflow_run") if isinstance(row, dict) else None
if (row.get("name") != sys.argv[2] or row.get("expired") is not False
    or not isinstance(workflow, dict) or str(workflow.get("id")) != sys.argv[3]
    or type(row.get("id")) is not int or row["id"] <= 0
    or re.fullmatch(r"sha256:[0-9a-f]{64}", row.get("digest", "")) is None):
    raise SystemExit("Security route artifact does not bind to the requested run")
print(row["id"])
PY
)"
python3 -I -B - "$ARTIFACT_DIR" "$ARTIFACT_DIR/build-artifacts.json" "$BUILD_NAME" "$BUILD_RUN_ID" "$ARTIFACT_DIR/security-artifacts.json" "$SECURITY_ROUTE_NAME" "$SECURITY_RUN_ID" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
root = Path(sys.argv[1])
if subprocess.check_output(["findmnt", "-n", "-T", str(root), "-o", "FSTYPE"], text=True).strip() != "tmpfs":
    raise SystemExit("artifact staging is not on tmpfs")
options = set(subprocess.check_output(["findmnt", "-n", "-T", str(root), "-o", "OPTIONS"], text=True).strip().split(","))
if not {"nosuid", "nodev"}.issubset(options):
    raise SystemExit("artifact tmpfs is missing nosuid or nodev")
def artifact_size(path: Path, name: str, run_id: str, maximum_bytes: int) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("artifacts") if isinstance(payload, dict) else None
    if payload.get("total_count") != 1 or not isinstance(rows, list) or len(rows) != 1:
        raise SystemExit("artifact staging metadata is not singular")
    row = rows[0]
    workflow = row.get("workflow_run") if isinstance(row, dict) else None
    size = row.get("size_in_bytes") if isinstance(row, dict) else None
    if (row.get("name") != name or not isinstance(workflow, dict)
            or str(workflow.get("id")) != run_id or type(size) is not int
            or not 0 < size <= maximum_bytes):
        raise SystemExit("artifact staging size or run binding is invalid")
    return size
zip_bytes = artifact_size(Path(sys.argv[2]), sys.argv[3], sys.argv[4], 262_144_000)
zip_bytes += artifact_size(Path(sys.argv[5]), sys.argv[6], sys.argv[7], 8 * 1024 * 1024)
filesystem = os.statvfs(root)
tmpfs_available = filesystem.f_bavail * filesystem.f_frsize
if tmpfs_available < zip_bytes + 64 * 1024 * 1024:
    raise SystemExit("tmpfs lacks room for both raw ZIPs and private metadata")
memory_available = None
for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
    if line.startswith("MemAvailable:"):
        memory_available = int(line.split()[1]) * 1024
        break
uncompressed_bytes = 262_144_000 + 65_536 + 1_048_576
memory_required = zip_bytes + uncompressed_bytes + 256 * 1024 * 1024
if memory_available is None or memory_available < memory_required:
    raise SystemExit("available memory is insufficient for bounded artifact verification")
PY
gh api --silent --header 'Accept: application/vnd.github+json' \
  "/repos/$REPO/actions/artifacts/$BUILD_ARTIFACT_ID/zip" > "$ARTIFACT_DIR/build-artifact.zip"
gh api --silent --header 'Accept: application/vnd.github+json' \
  "/repos/$REPO/actions/artifacts/$SECURITY_ARTIFACT_ID/zip" > "$ARTIFACT_DIR/security-route.zip"
REPO_ROOT='<clean checkout at exact PR head SHA>'
python3 -I -B "$REPO_ROOT/platform/tools/codex_archive_retention/verify_artifact.py" \
  --source-root "$REPO_ROOT" --pull-request "$PR" --head-sha "$HEAD_SHA" --merge-sha "$MERGE_SHA" \
  --build-run-id "$BUILD_RUN_ID" --build-attempt "$BUILD_ATTEMPT" \
  --build-run "$ARTIFACT_DIR/build-run.json" --build-artifacts "$ARTIFACT_DIR/build-artifacts.json" \
  --build-zip "$ARTIFACT_DIR/build-artifact.zip" \
  --security-run-id "$SECURITY_RUN_ID" --security-attempt "$SECURITY_ATTEMPT" \
  --security-run "$ARTIFACT_DIR/security-run.json" --security-artifacts "$ARTIFACT_DIR/security-artifacts.json" \
  --security-zip "$ARTIFACT_DIR/security-route.zip" \
  --extract-directory "$ARTIFACT_DIR/extracted" \
  --activation-output "$ARTIFACT_DIR/codex.activation.json"
```

The verifier binds successful attempts and the full/non-fallback/non-deployable
route, matches both raw ZIPs to API digests, validates closed members and
binary/source identity without execution, then extracts three members mode
`0600` under a private mode-`0700` directory and writes the API-bound receipt.
It rejects missing, expired, duplicate, stale, malformed, or mismatched input;
do not continue from partial downloads. Keep staging private until review and install decision. The receipt preserves the exact route reason and
runtime-sensitive value. Its closed schema is:

```json
{
  "schema_version": 1,
  "platform_repository": "StrayForest/old_sparky",
  "pull_request": "<PR number>",
  "platform_source_sha": "<PR head SHA>",
  "tested_merge_sha": "<tested merge SHA>",
  "build_run_id": "<native build run ID>",
  "build_run_attempt": "<native build attempt>",
  "build_artifact_id": "<GitHub build artifact ID>",
  "build_artifact_sha256": "<GitHub artifact digest without sha256:>",
  "security_workflow_name": "Platform security and build",
  "security_workflow_path": ".github/workflows/platform-security.yml",
  "security_run_id": "<Security run ID>",
  "security_run_attempt": "<Security attempt>",
  "security_conclusion": "success",
  "security_target_sha": "<tested merge SHA>",
  "security_route_artifact_id": "<GitHub route artifact ID>",
  "security_route_sha256": "<GitHub route artifact digest without sha256:>",
  "classifier_class": "full",
  "classifier_reason": "<exact reason emitted by the verifier>",
  "classifier_runtime_sensitive": false,
  "classifier_fallback": false,
  "classifier_deployable": false
}
```

The verifier writes mode `0600`; install root-owned mode `0600`. The service
wrapper rejects a missing, writable, malformed, or mismatched receipt before
enumerating candidates or invoking the binary.

Install after review and receipt verification using a clean exact-head
`REPO_ROOT` and the unit's root-owned `INSTALL_ROOT`. Check disk admission
before quiescing, preserve rollback files privately, and never execute from
`/dev/shm`. Keep one root shell for all blocks because later steps reuse
validated variables.

The trusted pre-quiesce ops snapshot records timer enabled/active and the
legacy static service failed. Current timer must be enabled/inactive. Capture
fresh service MainPID/cgroup metadata and require no process before stopping
both units. Clear only the service failure; then require inactive state and an
empty cgroup. Unknown or transitional states stop. Rollback restores the
original timer without directly starting the legacy service.

```bash
set -euo pipefail
test "$(id -u)" -eq 0
REPO_ROOT='<clean checkout at reviewed PR head SHA>'
PLATFORM_SOURCE_SHA='<PR head SHA from codex.manifest.json>'
MERGE_SHA='<tested merge SHA from the verified activation receipt>'
ARTIFACT_DIR='<private tmpfs directory containing raw ZIPs, extracted members, and receipt>'
BUILD_RUN_ID='<successful native build run ID>'
BUILD_ATTEMPT='<native build run attempt>'
OPS_SNAPSHOT_DIR='<reviewed root-private pre-quiesce unit snapshot directory>'
OPS_STATE_SHA256='<state.before.txt SHA-256 from the trusted operations handoff>'
INSTALL_ROOT=/usr/local/libexec/codex-archive-retention
ROLLBACK_DIR="/root/codex-archive-retention-rollback-${BUILD_RUN_ID}-${BUILD_ATTEMPT}"
CANDIDATE="$INSTALL_ROOT/.codex-${BUILD_RUN_ID}-${BUILD_ATTEMPT}.new"
[[ "$BUILD_RUN_ID" =~ ^[1-9][0-9]{0,31}$ && "$BUILD_ATTEMPT" =~ ^[1-9][0-9]{0,31}$ ]]
[[ "$OPS_STATE_SHA256" =~ ^[0-9a-f]{64}$ ]]
test -d "$OPS_SNAPSHOT_DIR"
test ! -L "$OPS_SNAPSHOT_DIR"
test "$(stat -c '%u' "$OPS_SNAPSHOT_DIR")" = 0
python3 -I -B - "$OPS_SNAPSHOT_DIR" <<'PY'
from pathlib import Path
import stat
import sys
root = Path(sys.argv[1])
info = root.lstat()
if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
    raise SystemExit("operations snapshot directory is not root-private")
for name in ("state.before.txt", "unit-sha256.txt", "codex-archive-retention.service", "codex-archive-retention.timer"):
    item = root / name
    item_info = item.lstat()
    if not stat.S_ISREG(item_info.st_mode) or item_info.st_uid != 0 or stat.S_IMODE(item_info.st_mode) & 0o077:
        raise SystemExit("operations snapshot member identity or mode is unsafe")
PY
test "$(sha256sum "$OPS_SNAPSHOT_DIR/state.before.txt" | awk '{print $1}')" = "$OPS_STATE_SHA256"
grep -Fxq 'service_enabled=static' "$OPS_SNAPSHOT_DIR/state.before.txt"
grep -Fxq 'timer_enabled=enabled' "$OPS_SNAPSHOT_DIR/state.before.txt"
grep -Fxq 'service_active=failed' "$OPS_SNAPSHOT_DIR/state.before.txt"
grep -Fxq 'timer_active=active' "$OPS_SNAPSHOT_DIR/state.before.txt"
(cd "$OPS_SNAPSHOT_DIR" && sha256sum --check --status unit-sha256.txt)
cmp -s /etc/systemd/system/codex-archive-retention.service \
  "$OPS_SNAPSHOT_DIR/codex-archive-retention.service"
cmp -s /etc/systemd/system/codex-archive-retention.timer \
  "$OPS_SNAPSHOT_DIR/codex-archive-retention.timer"
test "$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit})" = "$PLATFORM_SOURCE_SHA"
test -z "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all)"
test "$(stat -c '%u:%a' "$INSTALL_ROOT")" = '0:755'
if test -e "$INSTALL_ROOT/codex" || test -L "$INSTALL_ROOT/codex"; then
  echo "private Codex target already exists; stop for a reviewed replacement plan" >&2
  exit 1
fi
if test -e "$INSTALL_ROOT/patches" || test -L "$INSTALL_ROOT/patches"; then
  echo "private patch directory already exists; stop for a reviewed replacement plan" >&2
  exit 1
fi
for path in \
  "$INSTALL_ROOT/codex.manifest.json" "$INSTALL_ROOT/codex.activation.json" \
  "$INSTALL_ROOT/codex-source.lock.json" "$INSTALL_ROOT/patches/codex-0.166.0.patch"; do
  if test -e "$path" || test -L "$path"; then
    echo "unexpected existing companion support file; stop for a reviewed replacement plan" >&2
    exit 1
  fi
done
python3 -I -B - "$INSTALL_ROOT" "$CANDIDATE" <<'PY'
import os
from pathlib import Path
import stat
import sys
install_root, candidate = map(Path, sys.argv[1:])
root_info = install_root.lstat()
if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode) or root_info.st_uid != 0:
    raise SystemExit("private install root identity is invalid")
if candidate.parent != install_root:
    raise SystemExit("candidate path is outside the private install root")
try:
    candidate.lstat()
except FileNotFoundError:
    pass
else:
    raise SystemExit("candidate path already exists; stop for review")
PY
python3 -I -B - "$REPO_ROOT" "$INSTALL_ROOT" "$ARTIFACT_DIR/extracted/codex" \
  "$ARTIFACT_DIR/extracted/codex.manifest.json" "$ARTIFACT_DIR/codex.activation.json" \
  "$REPO_ROOT/platform/tools/codex_archive_retention/cleanup.py" \
  "$REPO_ROOT/platform/tools/codex_archive_retention/codex-source.lock.json" \
  "$REPO_ROOT/platform/tools/codex_archive_retention/patches/codex-0.166.0.patch" \
  "$REPO_ROOT/platform/tools/codex_archive_retention/codex-archive-retention.service" \
  "$REPO_ROOT/platform/tools/codex_archive_retention/codex-archive-retention.timer" \
  /etc/systemd/system/codex-archive-retention.service \
  /etc/systemd/system/codex-archive-retention.timer "$INSTALL_ROOT/cleanup.py" <<'PY'
import importlib.util
from pathlib import Path
import sys
repository, install_root, *payloads = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location(
    "codex_retention_verifier",
    repository / "platform/tools/codex_archive_retention/verify_artifact.py",
)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load the source-bound artifact verifier")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.check_persistent_install_space(install_root, payloads)
print("persistent install capacity admission passed")
PY
TIMER_ENABLED_CURRENT="$(systemctl is-enabled codex-archive-retention.timer 2>/dev/null || true)"
TIMER_ACTIVE_CURRENT="$(systemctl is-active codex-archive-retention.timer 2>/dev/null || true)"
SERVICE_STATE_BEFORE="$(systemctl is-active codex-archive-retention.service 2>/dev/null || true)"
SERVICE_MAIN_PID_BEFORE="$(systemctl show --property=MainPID --value codex-archive-retention.service)"
SERVICE_CGROUP_BEFORE="$(systemctl show --property=ControlGroup --value codex-archive-retention.service)"
test "$TIMER_ENABLED_CURRENT" = enabled
test "$TIMER_ACTIVE_CURRENT" = inactive
case "$SERVICE_STATE_BEFORE" in failed|inactive) ;; *) echo "service state is unsupported; abort" >&2; exit 1 ;; esac
test "$SERVICE_MAIN_PID_BEFORE" = 0
if test -n "$SERVICE_CGROUP_BEFORE"; then
  test "$SERVICE_CGROUP_BEFORE" = /system.slice/codex-archive-retention.service
  if test -s "/sys/fs/cgroup${SERVICE_CGROUP_BEFORE}/cgroup.procs"; then
    echo "legacy service cgroup is not empty; abort before stopping or resetting it" >&2
    exit 1
  fi
fi
if test -e "$ROLLBACK_DIR" || test -L "$ROLLBACK_DIR"; then
  echo "private rollback path already exists" >&2
  exit 1
fi
test -f /etc/systemd/system/codex-archive-retention.service
test ! -L /etc/systemd/system/codex-archive-retention.service
test -f /etc/systemd/system/codex-archive-retention.timer
test ! -L /etc/systemd/system/codex-archive-retention.timer
sudo install -d -o root -g root -m 0700 "$ROLLBACK_DIR"
sudo install -o root -g root -m 0600 /etc/systemd/system/codex-archive-retention.service "$ROLLBACK_DIR/service.before"
sudo install -o root -g root -m 0600 /etc/systemd/system/codex-archive-retention.timer "$ROLLBACK_DIR/timer.before"
sudo install -o root -g root -m 0600 "$INSTALL_ROOT/cleanup.py" "$ROLLBACK_DIR/cleanup.before"
printf 'timer_enabled_current=%s\ntimer_active_current=%s\nservice_state_before=%s\nservice_main_pid_before=%s\nservice_cgroup_before=%s\nops_state_sha256=%s\n' \
  "$TIMER_ENABLED_CURRENT" "$TIMER_ACTIVE_CURRENT" "$SERVICE_STATE_BEFORE" \
  "$SERVICE_MAIN_PID_BEFORE" "$SERVICE_CGROUP_BEFORE" "$OPS_STATE_SHA256" \
  > "$ROLLBACK_DIR/unit-state.before"
chmod 600 "$ROLLBACK_DIR/unit-state.before"
sudo systemctl stop codex-archive-retention.timer
! systemctl is-active --quiet codex-archive-retention.timer
sudo systemctl stop codex-archive-retention.service
SERVICE_STATE_AFTER_STOP="$(systemctl is-active codex-archive-retention.service 2>/dev/null || true)"
if [[ "$SERVICE_STATE_AFTER_STOP" != inactive && "$SERVICE_STATE_AFTER_STOP" != failed ]]; then
  echo "archive-retention service did not stop into an expected state" >&2
  exit 1
fi
SERVICE_MAIN_PID_AFTER_STOP="$(systemctl show --property=MainPID --value codex-archive-retention.service)"
SERVICE_CGROUP_AFTER_STOP="$(systemctl show --property=ControlGroup --value codex-archive-retention.service)"
test "$SERVICE_MAIN_PID_AFTER_STOP" = 0
if test -n "$SERVICE_CGROUP_AFTER_STOP"; then
  test "$SERVICE_CGROUP_AFTER_STOP" = /system.slice/codex-archive-retention.service
  if test -s "/sys/fs/cgroup${SERVICE_CGROUP_AFTER_STOP}/cgroup.procs"; then
    echo "archive-retention service still has a process in its cgroup" >&2
    exit 1
  fi
fi
sudo systemctl reset-failed codex-archive-retention.service
test "$(systemctl is-active codex-archive-retention.service 2>/dev/null || true)" = inactive
test "$(systemctl show --property=MainPID --value codex-archive-retention.service)" = 0
SERVICE_CGROUP_AFTER_RESET="$(systemctl show --property=ControlGroup --value codex-archive-retention.service)"
if test -n "$SERVICE_CGROUP_AFTER_RESET"; then
  test "$SERVICE_CGROUP_AFTER_RESET" = /system.slice/codex-archive-retention.service
  test ! -s "/sys/fs/cgroup${SERVICE_CGROUP_AFTER_RESET}/cgroup.procs"
fi
python3 -I -B - "$INSTALL_ROOT" "$CANDIDATE" <<'PY'
from pathlib import Path
import sys
install_root, candidate = map(Path, sys.argv[1:])
if candidate.parent != install_root:
    raise SystemExit("candidate path is outside the private install root")
try:
    candidate.lstat()
except FileNotFoundError:
    pass
else:
    raise SystemExit("candidate path appeared during quiesce; abort before writing")
PY
sudo install -d -o root -g root -m 0755 "$INSTALL_ROOT/patches"
sudo install -o root -g root -m 0600 "$ARTIFACT_DIR/extracted/codex" "$CANDIDATE"
python3 -I -B - "$REPO_ROOT" "$PLATFORM_SOURCE_SHA" "$MERGE_SHA" \
  "$CANDIDATE" \
  "$ARTIFACT_DIR/extracted/codex.manifest.json" "$ARTIFACT_DIR/codex.activation.json" \
  "$ARTIFACT_DIR/build-artifact.zip" "$ARTIFACT_DIR/security-route.zip" \
  "$INSTALL_ROOT" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
repository = Path(sys.argv[1])
expected_head, expected_merge = sys.argv[2:4]
candidate, manifest_path, activation_path, build_zip, security_zip, install_root = map(Path, sys.argv[4:])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
activation = json.loads(activation_path.read_text(encoding="utf-8"))
lock = json.loads((repository / "platform/tools/codex_archive_retention/codex-source.lock.json").read_text(encoding="utf-8"))
candidate_info = candidate.lstat()
manifest_info = manifest_path.lstat()
activation_info = activation_path.lstat()
build_zip_info = build_zip.lstat()
security_zip_info = security_zip.lstat()
with candidate.open("rb") as stream:
    candidate_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
with build_zip.open("rb") as stream:
    build_zip_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
with security_zip.open("rb") as stream:
    security_zip_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
if (not stat.S_ISREG(candidate_info.st_mode)
        or candidate_info.st_uid != 0 or candidate_info.st_mode & 0o077
        or not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_uid != 0
        or stat.S_IMODE(manifest_info.st_mode) != 0o600
        or not stat.S_ISREG(activation_info.st_mode) or activation_info.st_uid != 0
        or stat.S_IMODE(activation_info.st_mode) != 0o600
        or not stat.S_ISREG(build_zip_info.st_mode) or build_zip_info.st_uid != 0
        or not stat.S_ISREG(security_zip_info.st_mode) or security_zip_info.st_uid != 0
        or candidate_info.st_size != manifest["binary_bytes"]
        or candidate_sha256 != manifest["binary_sha256"]
        or build_zip_sha256 != activation["build_artifact_sha256"]
        or security_zip_sha256 != activation["security_route_sha256"]
        or activation["platform_repository"] != "StrayForest/old_sparky"
        or activation["pull_request"] != manifest["platform_pr_number"]
        or manifest["platform_source_sha"] != expected_head
        or manifest["platform_source_sha"] != activation["platform_source_sha"]
        or manifest["platform_tested_merge_sha"] != expected_merge
        or manifest["platform_tested_merge_sha"] != activation["tested_merge_sha"]
        or manifest["platform_event"] != "pull_request"
        or manifest["platform_ref"] != f"refs/pull/{manifest['platform_pr_number']}/merge"
        or manifest["platform_base_ref"] != "dev"
        or manifest["platform_head_repository"] != "StrayForest/old_sparky"
        or manifest["workflow_run_id"] != activation["build_run_id"]
        or manifest["workflow_run_attempt"] != activation["build_run_attempt"]
        or activation["classifier_class"] != "full"
        or activation["security_workflow_name"] != "Platform security and build"
        or activation["security_workflow_path"] != ".github/workflows/platform-security.yml"
        or activation["security_conclusion"] != "success"
        or activation["security_target_sha"] != expected_merge
        or activation["classifier_reason"] not in {
            "trusted candidate-packaging change requires full verification and is non-deployable",
            "platform or workflow change requires the full deterministic suite",
        }
        or type(activation["classifier_runtime_sensitive"]) is not bool
        or activation["classifier_fallback"] is not False
        or activation["classifier_deployable"] is not False
        or manifest["source_repository"] != lock["source_repository"]
        or manifest["source_tag"] != lock["source_tag"]
        or manifest["source_commit"] != lock["source_commit"]
        or manifest["target"] != lock["target"]
        or manifest["rust_toolchain"] != lock["rust_toolchain"]
        or manifest["cli_version"] != lock["cli_version"]
        or manifest["patch_sha256"] != lock["patch_sha256"]
        or manifest["binary_bytes"] > lock["max_binary_bytes"]):
    raise SystemExit("persistent candidate identity or digest check failed")
spec = os.statvfs(install_root)
block_size = spec.f_frsize
floor = max(5 * 1024**3, (spec.f_blocks * block_size * 10 + 99) // 100)
if spec.f_bavail * block_size < floor:
    raise SystemExit("copy crossed the shared free-space floor; do not publish candidate")
PY
sudo chmod 0755 "$CANDIDATE"
sudo ln -- "$CANDIDATE" "$INSTALL_ROOT/codex"
sudo rm -- "$CANDIDATE"
sudo install -o root -g root -m 0644 "$ARTIFACT_DIR/extracted/codex.manifest.json" "$INSTALL_ROOT/codex.manifest.json"
sudo install -o root -g root -m 0600 "$ARTIFACT_DIR/codex.activation.json" "$INSTALL_ROOT/codex.activation.json"
sudo install -o root -g root -m 0755 "$REPO_ROOT/platform/tools/codex_archive_retention/cleanup.py" "$INSTALL_ROOT/cleanup.py"
sudo install -o root -g root -m 0644 "$REPO_ROOT/platform/tools/codex_archive_retention/codex-source.lock.json" "$INSTALL_ROOT/codex-source.lock.json"
sudo install -o root -g root -m 0644 "$REPO_ROOT/platform/tools/codex_archive_retention/patches/codex-0.166.0.patch" "$INSTALL_ROOT/patches/codex-0.166.0.patch"
sudo install -o root -g root -m 0644 "$REPO_ROOT/platform/tools/codex_archive_retention/codex-archive-retention.service" /etc/systemd/system/codex-archive-retention.service
sudo install -o root -g root -m 0644 "$REPO_ROOT/platform/tools/codex_archive_retention/codex-archive-retention.timer" /etc/systemd/system/codex-archive-retention.timer
python3 -I -B - "$REPO_ROOT" "$INSTALL_ROOT" <<'PY'
import importlib.util
from pathlib import Path
import sys
repository, install_root = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location(
    "codex_retention_verifier",
    repository / "platform/tools/codex_archive_retention/verify_artifact.py",
)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load the source-bound artifact verifier")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.check_persistent_install_space(install_root, [])
print("installed companion remains above the shared free-space floor")
PY
```

Then validate and enable the timer:

```bash
sudo systemd-analyze verify /etc/systemd/system/codex-archive-retention.service \
  /etc/systemd/system/codex-archive-retention.timer
test "$(sudo "$INSTALL_ROOT/codex" --version)" = "$(cat "$ARTIFACT_DIR/extracted/version.txt")"
sudo systemctl daemon-reload
sudo systemctl enable --now codex-archive-retention.timer
sudo systemctl is-active --quiet codex-archive-retention.timer
sudo systemctl list-timers codex-archive-retention.timer --no-pager
```

The root service can write only the Codex home and has no external credentials.
The wrapper holds a root-owned nonblocking lock, makes at most 16 native
attempts, and re-enumerates after each success so archived children precede
parents. A mode-`0600` metadata cursor rotates the starting ID between runs;
each ID is attempted once per invocation, and native refusals are deferred.
Invalid cursor or inconsistent state stops the run. The systemd sandbox makes
installed code read-only and prevents other service identities from writing it.

## Validate and abort

Inspect only unit status and bounded logs; never capture rollout data. A
healthy service exits zero and is `inactive (dead)`. Logs contain attempt,
delete, defer, and remaining row/file counts only. Do not edit the cursor while
the timer is enabled. Before accepting a run, compare active rows/files using
metadata only. On any verification or state failure, restore the exact old
wrapper/units and pre-quiesce timer state from the ops snapshot. Clear the
legacy failure record but do not directly start that service.

```bash
set -euo pipefail
test "$(id -u)" -eq 0
test "$(sha256sum "$OPS_SNAPSHOT_DIR/state.before.txt" | awk '{print $1}')" = "$OPS_STATE_SHA256"
grep -Fxq 'timer_enabled=enabled' "$OPS_SNAPSHOT_DIR/state.before.txt"
grep -Fxq 'timer_active=active' "$OPS_SNAPSHOT_DIR/state.before.txt"
(cd "$OPS_SNAPSHOT_DIR" && sha256sum --check --status unit-sha256.txt)
sudo systemctl stop codex-archive-retention.timer
! systemctl is-active --quiet codex-archive-retention.timer
sudo systemctl stop codex-archive-retention.service
SERVICE_STATE_AFTER_STOP="$(systemctl is-active codex-archive-retention.service 2>/dev/null || true)"
if [[ "$SERVICE_STATE_AFTER_STOP" != inactive ]]; then
  echo "new archive-retention service did not become inactive; abort rollback before replacing files" >&2
  exit 1
fi
test "$(systemctl show --property=MainPID --value codex-archive-retention.service)" = 0
SERVICE_CGROUP="$(systemctl show --property=ControlGroup --value codex-archive-retention.service)"
if test -n "$SERVICE_CGROUP"; then
  test "$SERVICE_CGROUP" = /system.slice/codex-archive-retention.service
  if test -s "/sys/fs/cgroup${SERVICE_CGROUP}/cgroup.procs"; then
    echo "new archive-retention service still has a process; abort rollback before replacing files" >&2
    exit 1
  fi
fi
sudo systemctl reset-failed codex-archive-retention.service
test "$(systemctl is-active codex-archive-retention.service 2>/dev/null || true)" = inactive
test "$(systemctl show --property=MainPID --value codex-archive-retention.service)" = 0
SERVICE_CGROUP_AFTER_RESET="$(systemctl show --property=ControlGroup --value codex-archive-retention.service)"
if test -n "$SERVICE_CGROUP_AFTER_RESET"; then
  test "$SERVICE_CGROUP_AFTER_RESET" = /system.slice/codex-archive-retention.service
  test ! -s "/sys/fs/cgroup${SERVICE_CGROUP_AFTER_RESET}/cgroup.procs"
fi
sudo install -o root -g root -m 0644 "$ROLLBACK_DIR/service.before" /etc/systemd/system/codex-archive-retention.service
sudo install -o root -g root -m 0644 "$ROLLBACK_DIR/timer.before" /etc/systemd/system/codex-archive-retention.timer
sudo install -o root -g root -m 0755 "$ROLLBACK_DIR/cleanup.before" "$INSTALL_ROOT/cleanup.py"
sudo rm -f -- "$INSTALL_ROOT/codex" "$INSTALL_ROOT/codex.manifest.json" \
  "$INSTALL_ROOT/codex.activation.json" "$INSTALL_ROOT/codex-source.lock.json" \
  "$INSTALL_ROOT/patches/codex-0.166.0.patch"
if test -d "$INSTALL_ROOT/patches"; then
  sudo rmdir "$INSTALL_ROOT/patches"
fi
sudo systemctl daemon-reload
cmp -s /etc/systemd/system/codex-archive-retention.service \
  "$OPS_SNAPSHOT_DIR/codex-archive-retention.service"
cmp -s /etc/systemd/system/codex-archive-retention.timer \
  "$OPS_SNAPSHOT_DIR/codex-archive-retention.timer"
sudo systemctl reset-failed codex-archive-retention.service
sudo systemctl start codex-archive-retention.timer
sudo systemctl is-active --quiet codex-archive-retention.timer
test "$(systemctl is-enabled codex-archive-retention.timer)" = enabled
sudo systemctl status codex-archive-retention.service --no-pager
if test -e "$CANDIDATE" || test -L "$CANDIDATE"; then
  echo "preserving run-bound candidate for review: $CANDIDATE" >&2
fi
case "$ARTIFACT_DIR" in
  /dev/shm/codex-archive-retention.*) echo "preserving private evidence for review: $ARTIFACT_DIR" ;;
  *) echo "refusing to remove an unexpected artifact directory" >&2; exit 1 ;;
esac
case "$ROLLBACK_DIR" in
  /root/codex-archive-retention-rollback-*) echo "preserving rollback evidence for review: $ROLLBACK_DIR" ;;
  *) echo "refusing to use an unexpected rollback directory" >&2; exit 1 ;;
esac
```

On success, first confirm one completed zero-exit service run and a
metadata-only active-session comparison. Then remove only the private tmpfs
artifact directory; keep the rollback directory until the retention result is
accepted:

```bash
set -euo pipefail
test "$(id -u)" -eq 0
test "$(findmnt -n -T "$ARTIFACT_DIR" -o FSTYPE)" = tmpfs
test "$(stat -c '%u:%a' "$ARTIFACT_DIR")" = '0:700'
case "$ARTIFACT_DIR" in
  /dev/shm/codex-archive-retention.*) rm -rf --one-file-system -- "$ARTIFACT_DIR" ;;
  *) echo "refusing to remove an unexpected artifact directory" >&2; exit 1 ;;
esac
```

On abort, preserve the failed private artifact directory and rollback evidence
for review; do not restore archived rows or rollouts from guessed paths.
Resume only after the source, artifact, restored unit state, and local Codex
state have been revalidated. A later invocation retries candidates that were
refused or fell outside the per-run batch bound.
