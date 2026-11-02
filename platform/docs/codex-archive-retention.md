# Codex archived-session retention

This procedure owns the locally installed Codex archive-retention companion.
It acts only on sessions whose state database row is durably archived and
whose archived timestamp and canonical archived rollout identity agree. It
uses Codex's native archived-only delete path, which rechecks lifecycle state
and history references while holding Codex's writer locks. The Python wrapper
reads only thread IDs, archived flags and timestamps, rollout paths, spawn
edges, and file names and metadata. It never opens rollout contents or deletes
database rows or files directly.

The pinned upstream source and reviewed patch are declared in
[`../tools/codex_archive_retention/codex-source.lock.json`](../tools/codex_archive_retention/codex-source.lock.json).
The pinned patch also aligns the 170 local workspace package versions in
`codex-rs/Cargo.lock` with the exact source manifests (`0.166.0`). It leaves
third-party package records and dependency lists unchanged; native checks and
the release build continue to use `--locked`.
The companion is a private binary; it does not replace the system Codex
installation. The build workflow runs only for same-repository pull requests
targeting `dev`, with read-only repository permission. It checks out the exact
PR head, records the separately tested merge SHA, executes focused native
tests, and publishes a seven-day evidence artifact. It applies the reviewed
context-free patch with `git apply --unidiff-zero --whitespace=error-all` and
runs `git diff --check` on the patched checkout. Fork pull requests produce no
artifact. The workflow does not install software, enable a timer, read a Codex
home, or delete sessions.

## Build artifact and activation

Build evidence is review-only. Before activation, independently review the
exact platform source commit, upstream source commit, patch digest, native
test results, artifact manifest, wrapper behavior, and systemd boundaries.
Never install a candidate built from an unreviewed pull request. The artifact
manifest binds the platform source SHA, upstream tag and commit, patch digest,
Rust toolchain, target, CLI version, binary size, and binary digest.

Use only an artifact from this same-repository PR build after the canonical
`Platform security and build` workflow has completed successfully for the
same PR head and exact tested merge SHA. Confirm that the security classifier
route is `full`, `fallback=false`, and `deployable=false`, and that both the
security run and this native-build run are terminal successes for the expected
workflow attempts. Match the PR number, repository, base branch, head SHA,
merge SHA, upstream commit, patch digest, and artifact run/attempt before
reviewing or installing. Fetch exact attempt metadata and raw ZIPs; extracting
with `gh run download` alone is insufficient because it does not compare the
downloaded archive to GitHub's artifact digest.

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
ARTIFACT_DIR="$(mktemp -d /root/codex-archive-retention.XXXXXX)"
chmod 700 "$ARTIFACT_DIR"
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
  --activation-output "$ARTIFACT_DIR/codex.activation.json"
```

The verifier requires both exact workflow attempts to be terminal successes,
checks the Security classifier route as full/non-fallback/non-deployable,
matches both raw ZIP digests to GitHub's artifact API, validates the closed
ZIP member sets and binary/patch/source bindings without executing the binary,
and writes the activation receipt using those verified API values. It fails
closed on missing, expired, duplicate, stale, malformed, or mismatched inputs.
Do not continue from a partial download. Keep the artifact directory private
and remove it after the review and installation decision.

The verifier creates `codex.activation.json` only after verifying both
terminal workflow attempts and the classifier route. The reason and
runtime-sensitive fields carry the exact values from the verified route.
Its closed schema is:

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

The verifier writes the file with mode `0600`; install it root-owned mode
`0600`. The service wrapper rejects a missing, writable,
malformed, or mismatched activation receipt before enumerating candidates or
invoking the binary.

Install only after the independent review and activation receipt are complete.
Set `REPO_ROOT` to a clean checkout at the exact PR head SHA in the manifest:

```bash
set -euo pipefail
REPO_ROOT='<clean checkout at reviewed PR head SHA>'
PLATFORM_SOURCE_SHA='<PR head SHA from codex.manifest.json>'
ARTIFACT_DIR='<private directory containing codex, manifest, and activation receipt>'
INSTALL_ROOT=/usr/local/libexec/codex-archive-retention
test "$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit})" = "$PLATFORM_SOURCE_SHA"
test -z "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all)"
sudo install -d -o root -g root -m 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/patches"
sudo install -o root -g root -m 0755 "$ARTIFACT_DIR/codex" "$INSTALL_ROOT/codex"
sudo install -o root -g root -m 0644 "$ARTIFACT_DIR/codex.manifest.json" "$INSTALL_ROOT/codex.manifest.json"
sudo install -o root -g root -m 0600 "$ARTIFACT_DIR/codex.activation.json" "$INSTALL_ROOT/codex.activation.json"
sudo install -o root -g root -m 0755 "$REPO_ROOT/platform/tools/codex_archive_retention/cleanup.py" "$INSTALL_ROOT/cleanup.py"
sudo install -o root -g root -m 0644 "$REPO_ROOT/platform/tools/codex_archive_retention/codex-source.lock.json" "$INSTALL_ROOT/codex-source.lock.json"
sudo install -o root -g root -m 0644 "$REPO_ROOT/platform/tools/codex_archive_retention/patches/codex-0.166.0.patch" "$INSTALL_ROOT/patches/codex-0.166.0.patch"
sudo install -o root -g root -m 0644 "$REPO_ROOT/platform/tools/codex_archive_retention/codex-archive-retention.service" /etc/systemd/system/codex-archive-retention.service
sudo install -o root -g root -m 0644 "$REPO_ROOT/platform/tools/codex_archive_retention/codex-archive-retention.timer" /etc/systemd/system/codex-archive-retention.timer
```

Then validate and enable the timer:

```bash
sudo systemd-analyze verify /etc/systemd/system/codex-archive-retention.service \
  /etc/systemd/system/codex-archive-retention.timer
sudo systemctl daemon-reload
sudo systemctl enable --now codex-archive-retention.timer
sudo systemctl is-active --quiet codex-archive-retention.timer
sudo systemctl list-timers codex-archive-retention.timer --no-pager
case "$ARTIFACT_DIR" in
  /root/codex-archive-retention.*) sudo rm -rf -- "$ARTIFACT_DIR" ;;
  *) echo "refusing to remove an unexpected artifact directory" >&2; exit 1 ;;
esac
```

The service runs as root because `/root/.codex` is root-owned. Its systemd
sandbox makes the filesystem read-only except for that Codex home and gives
the service no external credentials. The wrapper takes a nonblocking
root-owned lock, makes at most 16 native delete attempts per invocation, and
re-enumerates after each deletion so archived child sessions are handled
before their parents. A root-owned mode-`0600` cursor in the Codex home rotates
the starting thread ID between runs, so a refused ID does not consume the whole
batch or starve later eligible rows. Each ID is attempted at most once per
invocation; native guard refusals are deferred to a later timer run. An invalid
cursor, inconsistent successful deletion, or unexpected state stops the run.
The service sandbox makes installed code read-only while it runs as root; no
other service identity can write the root-owned companion files.

## Validate and abort

After activation, inspect only unit status and bounded wrapper logs; do not
capture, print, or attach rollout data. A healthy completed run has service
status `inactive (dead)` and exit status zero. The log reports native attempt
count, deleted count, deferred count, and remaining archived row and file
counts only. The cursor is metadata-only scheduling state; do not edit or
remove it while the timer is enabled. Confirm that the active-session
collection and its state rows remain present using a
separate metadata-only check before accepting the first run.

If provenance validation, metadata validation, systemd verification, or the
post-run state check fails, stop and disable the timer before any repair:

```bash
systemctl disable --now codex-archive-retention.timer
systemctl stop codex-archive-retention.service
systemctl status codex-archive-retention.service --no-pager
case "$ARTIFACT_DIR" in
  /root/codex-archive-retention.*) sudo rm -rf -- "$ARTIFACT_DIR" ;;
  *) echo "refusing to remove an unexpected artifact directory" >&2; exit 1 ;;
esac
```

Preserve the binary, manifest, wrapper, and unit files for review; do not
restore archived rows or rollouts from guessed paths. Keep the legacy Codex
retention timer disabled. Resume only after the source, artifact, and local
Codex state have been revalidated. A later invocation retries candidates that
were refused or fell outside the per-run batch bound.
