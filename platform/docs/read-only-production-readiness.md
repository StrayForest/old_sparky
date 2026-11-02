# Read-only production readiness

- Status: Operator procedure
- Owner: Production operator
- Last reviewed: 2026-11-02

Use this procedure only to establish bounded read-only readiness evidence.
It cannot authorize deployment, runtime changes, fixture creation, load
execution or cleanup.

If the installed collector cannot complete because its own validation path is
the failing component, a separately reviewed read-only verifier may be staged
from its exact source commit into a private directory. This is an exceptional
readiness-only procedure: it never authorizes deployment, runtime changes,
fixture creation, load execution or cleanup. Do not run it until that exact
source has passed its required CI and independent source/command review. It
reports `verifier_source_sha` separately from the installed `app_target_sha`.
The following stages only the fixed collector and its two exact helper modules
from the named Git commit, checking Git file mode and SHA-256 against a private
manifest before execution:
The staging command uses `git ls-tree` to check exact tracked modes and
`git cat-file blob` to materialize only the three listed modules.

```bash
set -euo pipefail
umask 077
SOURCE_REPO=/root/old_sparky
VERIFIER_SOURCE_SHA='<exact-reviewed-source-commit>'
EXPECTED_APP_SHA='<active-app-sha-A>'
EXPECTED_PREVIOUS_APP_SHA='<previous-app-sha>'
EXPECTED_RELEASE_MANIFEST_SHA='<active-release-manifest-sha-from-deploy-receipt>'
EXPECTED_PREVIOUS_RELEASE_MANIFEST_SHA='<previous-release-manifest-sha>'
EXPECTED_HOST_TOOLS_SHA='<host-tools-sha-C-from-active-pin>'
EXPECTED_PREVIOUS_HOST_TOOLS_SHA='<preserved-host-tools-sha>'
EVIDENCE_DIR="$(mktemp -d /root/platform-readiness-alternate.XXXXXX)"
test "$(/usr/bin/stat -c '%a' "$EVIDENCE_DIR")" = 700
STAGE_DIR="$EVIDENCE_DIR/verifier"
test ! -e "$STAGE_DIR" && test ! -L "$STAGE_DIR"
/usr/bin/python3.12 -I -B - "$SOURCE_REPO" "$VERIFIER_SOURCE_SHA" "$STAGE_DIR" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

repo = Path(sys.argv[1])
source_sha = sys.argv[2]
stage = Path(sys.argv[3])
files = (
    "platform/tools/platform_readonly_live_readiness.py",
    "platform/tools/platform_host_tools_pin.py",
    "platform/tools/platform_host_tools_bundle.py",
)
if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
    raise SystemExit("invalid verifier source SHA")
if stage.exists() or stage.is_symlink() or not stage.parent.is_dir():
    raise SystemExit("unsafe verifier staging path")
commit = subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "--verify", f"{source_sha}^{{commit}}"],
    check=True, capture_output=True, text=True,
).stdout.strip()
if commit != source_sha:
    raise SystemExit("verifier commit identity mismatch")
stage.mkdir(mode=0o700)
entries = []
for relative in files:
    row = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-z", "--full-tree", source_sha, "--", relative],
        check=True, capture_output=True,
    ).stdout
    if not row.endswith(b"\0") or row.count(b"\0") != 1:
        raise SystemExit("verifier Git tree entry is missing or ambiguous")
    header, path = row[:-1].split(b"\t", 1)
    mode, kind, oid = header.decode("ascii").split(" ")
    if path.decode("utf-8") != relative or mode != "100644" or kind != "blob":
        raise SystemExit("verifier Git tree mode or path differs")
    contents = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", oid],
        check=True, capture_output=True,
    ).stdout
    if not contents or len(contents) > 512 * 1024:
        raise SystemExit("verifier module size is outside its bound")
    target = stage / relative
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.fchmod(descriptor, 0o644)
    finally:
        os.close(descriptor)
    metadata = target.lstat()
    digest = hashlib.sha256(contents).hexdigest()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o644
        or metadata.st_size != len(contents)
        or hashlib.sha256(target.read_bytes()).hexdigest() != digest
    ):
        raise SystemExit("staged verifier module failed metadata/hash verification")
    entries.append({"path": relative, "mode": "100644", "size": len(contents), "sha256": digest, "git_blob": oid})
manifest = {"schema": 1, "verifier_source_sha": source_sha, "files": entries}
manifest_path = stage / "manifest.json"
manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "wb") as stream:
    stream.write(manifest_bytes)
    stream.flush()
    os.fsync(stream.fileno())
PY
test "$(/usr/bin/stat -c '%a' "$STAGE_DIR/manifest.json")" = 600
/usr/bin/python3.12 -I -B - "$STAGE_DIR" "$VERIFIER_SOURCE_SHA" \
  > "$EVIDENCE_DIR/verifier-stage-verification.json" <<'PY'
import hashlib
import json
from pathlib import Path
import stat
import sys

stage = Path(sys.argv[1])
source_sha = sys.argv[2]
expected = (
    "platform/tools/platform_readonly_live_readiness.py",
    "platform/tools/platform_host_tools_pin.py",
    "platform/tools/platform_host_tools_bundle.py",
)
def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate manifest key")
        value[key] = item
    return value
manifest = json.loads((stage / "manifest.json").read_text(encoding="ascii"), object_pairs_hook=pairs)
if set(manifest) != {"schema", "verifier_source_sha", "files"} or manifest["schema"] != 1 or manifest["verifier_source_sha"] != source_sha:
    raise SystemExit("verifier manifest identity differs")
if tuple(entry.get("path") for entry in manifest["files"]) != expected:
    raise SystemExit("verifier manifest path set or order differs")
for entry in manifest["files"]:
    if set(entry) != {"path", "mode", "size", "sha256", "git_blob"} or entry["mode"] != "100644":
        raise SystemExit("verifier manifest entry is not closed")
    path = stage / entry["path"]
    metadata = path.lstat()
    data = path.read_bytes()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o644 or len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise SystemExit("staged verifier does not match its manifest")
print(json.dumps({"schema": 1, "status": "verified", "verifier_source_sha": source_sha, "files": len(expected)}, sort_keys=True, separators=(",", ":")))
PY
test "$(/usr/bin/stat -c '%a' "$EVIDENCE_DIR/verifier-stage-verification.json")" = 600
/usr/bin/python3.12 -I -B \
  "$STAGE_DIR/platform/tools/platform_readonly_live_readiness.py" \
  --verifier-source-sha "$VERIFIER_SOURCE_SHA" \
  --verifier-manifest "$STAGE_DIR/manifest.json" \
  --expected-app-sha "$EXPECTED_APP_SHA" \
  --expected-previous-app-sha "$EXPECTED_PREVIOUS_APP_SHA" \
  --expected-release-manifest-sha "$EXPECTED_RELEASE_MANIFEST_SHA" \
  --expected-previous-release-manifest-sha "$EXPECTED_PREVIOUS_RELEASE_MANIFEST_SHA" \
  --expected-host-tools-sha "$EXPECTED_HOST_TOOLS_SHA" \
  --expected-previous-host-tools-sha "$EXPECTED_PREVIOUS_HOST_TOOLS_SHA" \
  --expected-toolset-version production-host-tools-v4 \
  > "$EVIDENCE_DIR/readiness.json" 2> "$EVIDENCE_DIR/readiness.stderr"
test ! -s "$EVIDENCE_DIR/readiness.stderr"
test "$(/usr/bin/stat -c '%a' "$EVIDENCE_DIR/readiness.json")" = 600
/usr/bin/python3.12 -I -B -c \
  'import json,sys; d=json.load(open(sys.argv[1], encoding="utf-8")); assert d.get("status") == "ready" and d.get("verifier_source_sha") == sys.argv[2] and d.get("app_target_sha") == sys.argv[3] and d.get("runtime_or_fixture_mutations_authorized") is False' \
  "$EVIDENCE_DIR/readiness.json" "$VERIFIER_SOURCE_SHA" "$EXPECTED_APP_SHA"
```

Require exit 0 and `status=ready`; the JSON must bind `verifier_source_sha`
separately from installed `app_target_sha` and, for an alternate verifier,
include the verified `verifier_manifest_sha256`. It must also bind the supplied
current and previous app sources/release-manifest digests and C generation values, verify
all 15 pinned C members by digest and normalized manifest mode,
confirm that a directory exists at the expected previous C generation path
(presence only; it is not a verification of that prior generation's contents),
validate the current, previous and shared-venv origin identities, and show the expected allowlisted
service profile, HTTP 200 health, clear locks, no marker/plans/jobs, and the
enabled maintenance timer with its service terminal and idle. The workflow's
runner-only `host-capability-preflight` remains authoritative for its own
`RUNNER_TEMP` and SSH handoff; do not run
`platform_host_tools_installed_check.py` from the host shell. A terminal-idle
maintenance service is not proof that maintenance succeeded; use its exact
receipt for that claim.
The collector rechecks release pointers, service identities, locks, plans and
maintenance state immediately before returning `status=ready`; these checks
form a bounded observation, not an atomic snapshot.
