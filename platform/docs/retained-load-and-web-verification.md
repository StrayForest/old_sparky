# Retained-load and web verification

- Status: Active reference and operator how-to
- Owner: Performance and web verification owners
- Last reviewed: 2026-11-04

This document owns the detailed retained-load cleanup, hermetic web verification,
external-load workflow barrier and evidence-projection contracts. The
[operations runbook](operations-runbook.md) owns the surrounding operator
procedure and links here rather than duplicating these contracts.

## Retained-load cleanup and hermetic web verification

The exact retained-load supervisor deletes each selected tournament's four
rebuildable read-model keys (`teams`, `workspace_detail`, `bracket_summary`,
`bracket_full`) and rechecks them; zero residual keys is mandatory. A Redis
outage, failed delete or surviving key fails closed even though ordinary reads
may treat Redis as optional. It also verifies zero fixture users, tournaments,
sessions and audit rows while preserving the control account. The [cleanup
tool](../tools/platform_cleanup_retained_matrix.py) calls `dispose_engine()` and
`dispose_redis_clients()`; close warnings are failures, not harmless noise.

Use the [canonical retained-load abort workflow](../../.github/workflows/platform-production-retained-load-abort.yml) for the exact run/attempt; see the [SSR diagnostic runbook](ssr-capacity-diagnostic-runbook.md) for dispatch inputs and Security identity. If
`supervisor.exit` is absent, abort accepts only the latest exact failed run
with reviewed source/workflow, matching actor and complete jobs; it rechecks
the attempt around abort/normalization. Under release/retained locks, the helper
revalidates the marker and requires two clean zero-process scans at least
100 ms apart. Its schema-2 receipt records missing exit, null code and
`namespace_closed=false` but does not prove namespace closure; continue only after normalization and never create receipts by hand.
Inspect the exact-run finalizer receipt first; dispatch cleanup only if it is
missing or incomplete, not when it passed with zero residuals.

For setup failure with one recognized profile directory but no manifest, use
the same run-bound cleanup workflow after source/process/namespace barriers.
The installed supervisor checks runner/app binding, resolves its helper from
the active immutable app release, then reconstructs inventory from exactly one
durable `PreprodTestRun` row at the exact report path (origin, mode, marker,
scoped user count and tournament IDs). Standard cleanup validates the report
and summary before deletion. Never synthesize manifests, invoke helpers directly or use the empty-root no-op with fixture evidence; ambiguity fails. Read only the exact `control_email` field from retained private operator inputs, validate without printing, and enter it through the workflow's hidden prompt for private stdin forwarding. Use bounded `RETAINED_CLEANUP_STAGE` or cleanup diagnostics to classify child failure; generic outer lock messages are not diagnostic.

If the reviewed abort helper is on recovery-only runner SHA `R` while the active app is at freshly verified SHA `A`, use this order. Runner binding permits the same app SHA or an exact verified no-op receipt; the host helper checks the bound app SHA against current `RELEASE.json`.
For a deployable source not active, follow the [deploy-before-cleanup sequence in the SSR diagnostic runbook](ssr-capacity-diagnostic-runbook.md#abort-and-close-the-exact-fixture): abort → one source-bound backup → AUTO normal deployment/readiness → verify source and pin active → exact old-run cleanup. Keep load closed; do not override target, alias no-op, or start another workload.

Before starting Security or reserving its backup, inventory task-owned
temporary paths, sizes and open users. Remove only exact task artifacts whose
owner and lack of process references are verified; follow the [test-suite
storage guidance](test-suite-governance.md) and coordinate exact-path cleanup
with the release lead. Do not remove installed releases, shared runtimes,
current/previous releases, backup pairs or other protected paths. Then collect
fresh read-only host capacity evidence for active app `A`:

Use the exact active app SHA from the current successful, source-bound deployment
receipt. Assign that 40-character public SHA to `active_app_sha`; the workflow
checks it against the fresh active-release identity on the host. The placeholder
below is an input to replace, not a valid SHA value:

```bash
set -euo pipefail
active_app_sha="<active-app-sha-from-current-deployment-receipt>"
[[ "$active_app_sha" =~ ^[0-9a-f]{40}$ ]]
gh workflow run platform-production-storage-diagnostics.yml \
  --repo StrayForest/old_sparky --ref dev \
  -f expected_sha="$active_app_sha"
gh run watch <storage-diagnostics-run-id> \
  --repo StrayForest/old_sparky --exit-status
```

Before reserving either backup, require the reported free-space budget to cover
the storage policy floor and backup lead, both preserved new backup archives
(recovery source `R` and the later app source), and the candidate release's
measured transfer/staging allocation. Use the newest verified archive and a
comparable release artifact for conservative size inputs; include any
temporary allocation that coexists with their final files. If an input is
unknown, the report is incomplete, or the budget is insufficient, stop before
starting Security and arrange reviewed exact-path cleanup or a later window.
The diagnostic is a snapshot, not a reservation; the backup and deployment
guards still decide admission at execution time. Do not use the diagnostics'
generic release-candidate list for deletion. For any maintenance review, use
the [pin-bearing no-apply retention preview](operations-runbook.md#retention-and-disk-safety),
which validates the installed unit and includes its protected pins.

1. Start `Platform security and build` for the merged recovery-only source SHA
   `R` on `dev`.
2. While that exact Security run is active and the shared production backup
   slot is unowned, dispatch exactly one source-bound `operation=create`
   backup for the same Security run/attempt and `R`, using the existing
   [backup command and timing requirements](backup-restore-runbook.md#local-verified-backup).
   Confirm both the backup workflow and its production job start before the
   latest required Security job completes. Reserve and start it before waiting
   for Security success or an AUTO receipt; AUTO only verifies the backup and
   does not dispatch it. Use the default `tmpfs_v1` backend for this `dev`
   source attempt.
3. Wait for exact Security success and the restore-verified backup receipt,
   then let the automatic baseline-reconcile chain finish. Verify its
   authenticated `platform-production-noop-source-receipt` binds the exact
   Security, auto-deploy and deploy run attempts, the cumulative no-op route,
   and the unchanged active-release tuple at `A` (`runtime_required=false`).

This receipt authorizes the runner-to-app source binding only; it does not
deploy the app. Then use the abort command in the SSR runbook with
`target_sha=A`, the exact failed load run and attempt, and the successful
Security run and attempt for `R`. The abort workflow validates the complete
Security job page against the exact CI route artifact. Every required gate must
pass; only the `Conditional release runtime fixture` and `Trusted dev immutable
release runtime` jobs may be skipped when that validated manifest is the full,
non-deployable recovery-bootstrap route with `runtime_sensitive=false`.
Deployable routes require both runtime jobs to pass, and no other skipped gate
is accepted. After the canonical abort receipt passes, dispatch exact cleanup
through the same normal source-binding path with runner `R` and app target
`A`; the cleanup helper rechecks the active tuple. Stop if the backup proof or
no-op receipt is absent, does not bind those exact attempts, or the active
tuple has changed. Do not create a substitute receipt or alias a same-SHA run.

Optional manual inspection of the Security route artifact is not an extra
authorization step; the abort workflow performs the authoritative validation.
For an independent archive check, run from the repository root of a checkout at
the exact successful Security source SHA `R`:

```bash
set -euo pipefail
security_run_id="<exact-successful-security-run-id>"
security_run_attempt="<exact-successful-security-run-attempt>"
security_source_sha="<security-run-source-sha-R>"
[[ "$security_run_id" =~ ^[1-9][0-9]{0,31}$ ]]
[[ "$security_run_attempt" =~ ^[1-9][0-9]{0,8}$ ]]
[[ "$security_source_sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git rev-parse --verify HEAD)" == "$security_source_sha" ]]
umask 077
private_dir="$(mktemp -d "${TMPDIR:-/tmp}/retained-abort-route.XXXXXX")"
trap 'rm -rf -- "$private_dir"' EXIT
metadata="$private_dir/artifacts.json"
archive="$private_dir/route.zip"
gh api \
  "repos/StrayForest/old_sparky/actions/runs/$security_run_id/artifacts?per_page=100&page=1" \
  > "$metadata"
artifact_fields="$(/usr/bin/python3 -I -B - "$metadata" "$security_run_id" "$security_run_attempt" "$security_source_sha" <<'PY'
import json
from pathlib import Path
import re
import sys

metadata_path, run_id, attempt, source_sha = sys.argv[1:]
payload = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
rows = payload.get("artifacts") if isinstance(payload, dict) else None
if (not isinstance(rows, list) or type(payload.get("total_count")) is not int
        or payload["total_count"] != len(rows) or not 0 < len(rows) <= 100):
    raise SystemExit("Security artifact list is incomplete")
expected_name = f"platform-ci-route-{run_id}-{attempt}"
matches = [row for row in rows if isinstance(row, dict) and row.get("name") == expected_name]
if len(matches) != 1:
    raise SystemExit("exact Security route artifact is missing or ambiguous")
artifact = matches[0]
workflow_run = artifact.get("workflow_run")
if (type(artifact.get("id")) is not int or artifact["id"] <= 0
        or type(artifact.get("size_in_bytes")) is not int
        or not 0 < artifact["size_in_bytes"] <= 4 * 1024 * 1024
        or artifact.get("expired") is not False
        or not isinstance(artifact.get("digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]) is None
        or not isinstance(workflow_run, dict)
        or type(workflow_run.get("id")) is not int
        or workflow_run["id"] != int(run_id)
        or workflow_run.get("head_sha") != source_sha
        or ("run_attempt" in workflow_run and
            (type(workflow_run["run_attempt"]) is not int
             or workflow_run["run_attempt"] != int(attempt)))):
    raise SystemExit("Security route artifact identity is not exact")
print(artifact["id"], artifact["size_in_bytes"], artifact["digest"][7:])
PY
)"
read -r artifact_id api_size api_sha256 <<< "$artifact_fields"
gh api \
  "repos/StrayForest/old_sparky/actions/artifacts/$artifact_id/zip" \
  > "$archive"
[[ "$(stat -c '%s' -- "$archive")" == "$api_size" ]]
[[ "$(sha256sum -- "$archive" | awk '{print $1}')" == "$api_sha256" ]]
/usr/bin/python3 -I -B platform/tools/platform_production_classifier_artifact.py \
  manifest "$archive" --target-sha "$security_source_sha" --require-recovery-bootstrap
```

The retained-cleanup workflow keeps SSH stdout/stderr in runner-private files
until it records the closed diagnostic status. Missing or invalid remote
markers remain unverified and fail closed; only allowlisted stderr classes and
bounded byte counts are projected, and raw captures are removed by the exit
trap.

The canonical [hermetic runner](../tools/platform_web_hermetic.sh) builds one
standalone Next artifact in a temporary immutable directory, runs the
source-only contract once, then runs smoke and participant suites sequentially
from that build. The source contract's
[`playwright.source-contract.config.ts`](../apps/platform_web/playwright.source-contract.config.ts)
has no `webServer`; smoke/participant use fresh API/web processes and
`reuseExistingServer: false`, with no ambient server/browser/database reuse.
Responsive ownership is `desktop` (1440), `wide-1300` (1300), `tablet-820`
(820) and `mobile-layout` (Pixel 5); the explicit desktop-only list runs only
in `desktop`, while participant has its own one-worker desktop/mobile projects
([configs](../apps/platform_web/playwright.config.ts), [participant
config](../apps/platform_web/playwright.participant.config.ts)).

Approved load generators may be listed in the exact-address setting
`PLATFORM_LOAD_TEST_SOURCE_IPS`. The allowlist skips only per-source/IP
throttles for authentication, invite and media paths; account, authenticated
user, byte, application-global and Nginx capacity limits remain active. It
accepts individual IPv4/IPv6 addresses only, never a CIDR or wildcard.

## External-load workflow and evidence

The workflow is fail-closed across separate runners; finalization, exact cleanup
and SSH removal use `always()`, but any failed row below keeps the run failed:

| Boundary | Passing value |
| --- | --- |
| dispatch, setup and load-client jobs | job result `success`; setup `setup_status=0` |
| candidate production | schema-2 receipt binds source/run/attempt/profile and exact report digest; `pending_origin` is accepted only for the load tool's reserved exit 3 after closed worker/namespace/binding checks, including a typed status-failure check when `contract_ok` is false |
| remote/finalization | `remote_status=0`, `observer_ready=1`, `finalize_status=0` |
| cleanup/export cleanup | `cleanup_status=0`, `cleanup_exports_status=0` |
| handoffs/artifacts/SSH | exact SHA/run/attempt/digest; cleanup statuses `0` |
| evaluation/projection | observer-bound accepted result, structurally complete profile-budget miss, or closed known-status failure; `sanitizer_status=0` |

`load-client` runs `platform/tools/platform_load.py export-source-binding-env` against the exact input and manifest, keeping validated values in a private
file for its same-step `env -i` call. `$GITHUB_ENV` affects later steps only.
Diagnostic mode uses the no-secret `derive-diagnostic-source-binding` job after
the exact same-run/attempt input and manifest are ready. It validates input
run/runner identity and manifest, then exposes only the bounded binding and
digest as outputs. Parent and sanitizer steps receive step-scoped env; SSH
setup/cleanup do not. The worker uses its explicit allowlist. Never set values
manually. Missing or mismatched binding stops acceptance; empty binding is valid
only when runner and app SHAs match. See the [source-binding test contract](test-suite-governance.md#production-and-performance-boundaries)
for required full-module verification and coverage.

### Retrieve a bounded SSR failure artifact

Fingerprint and sanitizer artifacts expire after one day. Use the authenticated GitHub API procedure below to verify the exact attempt and ZIP before reading the sole bounded member; repeat if both exist.

~~~bash
set -euo pipefail
umask 077
load_run_id="<exact-load-run-id>"
load_run_attempt="<exact-load-attempt>"
load_source_sha="<exact-load-runner-source-sha>"
artifact_kind="fingerprint" # or sanitizer
[[ "$load_run_id" =~ ^[1-9][0-9]{0,31}$ ]]
[[ "$load_run_attempt" =~ ^[1-9][0-9]{0,31}$ ]]
[[ "$load_source_sha" =~ ^[0-9a-f]{40}$ ]]
case "$artifact_kind" in
  fingerprint)
    artifact_prefix="platform-production-ssr-capacity-fingerprint"
    member_name="failure-fingerprint.json"; member_limit=16384 ;;
  sanitizer)
    artifact_prefix="platform-production-ssr-sanitizer-rejection"
    member_name="sanitizer-failure.json"; member_limit=262144 ;;
  *) echo "unknown artifact kind" >&2; exit 2 ;;
esac
artifact_name="$artifact_prefix-$load_run_id-$load_run_attempt"
private_dir="$(mktemp -d /tmp/ssr-failure-artifact.XXXXXX)"
chmod 700 "$private_dir"
run_file="$private_dir/run.json"
list_file="$private_dir/artifacts.json"
detail_file="$private_dir/artifact.json"
archive="$private_dir/artifact.zip"
member_file="$private_dir/$member_name"
gh api \
  "repos/StrayForest/old_sparky/actions/runs/$load_run_id/attempts/$load_run_attempt" > "$run_file"
gh api \
  "repos/StrayForest/old_sparky/actions/runs/$load_run_id/artifacts?per_page=100&page=1" > "$list_file"
fields="$(/usr/bin/python3 -I -B - "$run_file" "$list_file" "$load_run_id" \
  "$load_run_attempt" "$load_source_sha" "$artifact_name" <<'PY'
import json
import re
import sys
from pathlib import Path

run_path, list_path, run_id, attempt, source, name = sys.argv[1:]
run = json.loads(Path(run_path).read_text())
listing = json.loads(Path(list_path).read_text())
if (not isinstance(run, dict) or type(run.get("id")) is not int
        or type(run.get("run_attempt")) is not int or run.get("id") != int(run_id)
        or run.get("run_attempt") != int(attempt)
        or run.get("head_sha") != source or run.get("head_branch") != "dev"
        or run.get("path") != ".github/workflows/platform-production-external-load.yml"
        or run.get("event") != "workflow_dispatch" or run.get("status") != "completed"
        or run.get("conclusion") != "failure"):
    raise SystemExit("exact failed load attempt does not match")
rows = listing.get("artifacts") if isinstance(listing, dict) else None
if (not isinstance(rows, list) or type(listing.get("total_count")) is not int
        or listing["total_count"] != len(rows) or not 0 < len(rows) <= 100):
    raise SystemExit("artifact listing is incomplete or exceeds the page bound")
matches = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
if len(matches) != 1:
    raise SystemExit("exact failure artifact is missing or ambiguous")
a = matches[0]
w = a.get("workflow_run")
if (type(a.get("id")) is not int or a["id"] <= 0
        or type(a.get("size_in_bytes")) is not int or not 0 < a["size_in_bytes"] <= 4*1024*1024
        or a.get("expired") is not False or not isinstance(a.get("digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", a["digest"]) is None
        or not isinstance(w, dict) or type(w.get("id")) is not int
        or w.get("id") != int(run_id) or w.get("head_sha") != source
        or ("run_attempt" in w and (type(w["run_attempt"]) is not int
                                      or w["run_attempt"] != int(attempt)))):
    raise SystemExit("artifact metadata is not bound to the exact attempt")
print(a["id"], a["size_in_bytes"], a["digest"][7:])
PY
)"
read -r artifact_id api_size api_sha <<< "$fields"
gh api \
  "repos/StrayForest/old_sparky/actions/artifacts/$artifact_id" > "$detail_file"
gh api \
  "repos/StrayForest/old_sparky/actions/artifacts/$artifact_id/zip" > "$archive"
/usr/bin/python3 -I -B - "$detail_file" "$archive" "$member_file" \
  "$artifact_id" "$artifact_name" "$load_run_id" "$load_run_attempt" \
  "$load_source_sha" "$api_size" "$api_sha" "$member_name" "$member_limit" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import zipfile
detail_path, archive_path, member_path, artifact_id, name, run_id, attempt, source, size, digest, member, limit = sys.argv[1:]
a = json.loads(Path(detail_path).read_text())
w = a.get("workflow_run")
if (type(a.get("id")) is not int or a.get("id") != int(artifact_id)
        or a.get("name") != name
        or a.get("size_in_bytes") != int(size) or a.get("digest") != "sha256:" + digest
        or a.get("expired") is not False or not isinstance(w, dict)
        or type(w.get("id")) is not int or w.get("id") != int(run_id)
        or w.get("head_sha") != source
        or ("run_attempt" in w and (type(w["run_attempt"]) is not int
                                      or w["run_attempt"] != int(attempt)))):
    raise SystemExit("artifact detail does not match exact API listing/run")
raw = Path(archive_path).read_bytes()
if (len(raw) != int(size) or len(raw) > 4*1024*1024
        or hashlib.sha256(raw).hexdigest() != digest):
    raise SystemExit("outer ZIP size or digest mismatch")
with zipfile.ZipFile(archive_path) as z:
    rows = z.infolist()
    if len(rows) != 1 or rows[0].filename != member:
        raise SystemExit("ZIP member set is not exact")
    info = rows[0]
    if (info.is_dir() or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)
            or info.file_size <= 0 or info.file_size > int(limit)):
        raise SystemExit("ZIP member type or size is invalid")
    with z.open(info) as source_file:
        data = source_file.read(int(limit) + 1)
        if len(data) > int(limit) or source_file.read(1):
            raise SystemExit("ZIP member exceeds its bound")
if len(data) != info.file_size:
    raise SystemExit("ZIP member length is inconsistent")
fd = os.open(member_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd, "wb", closefd=False) as output:
        output.write(data); output.flush(); os.fsync(output.fileno())
finally:
    os.close(fd)
print(f"verified {name}; extracted only {member} ({len(data)} bytes)")
PY
python3 - "$artifact_kind" "$member_file" "$load_run_id" "$load_run_attempt" "$load_source_sha" <<'PY'
import json
import re
import sys
from pathlib import Path
kind, path, run_id, attempt, source = sys.argv[1:]
p = json.loads(Path(path).read_text(encoding="ascii"))
if kind == "fingerprint":
    if (not isinstance(p, dict) or p.get("event") != "ssr_capacity_composite_parent_fingerprint"
            or p.get("workload") != "authenticated_html_ssr_capacity_120s_plus_cpu_profile_20s_v1"
            or p.get("external_run_id") != run_id or p.get("external_run_attempt") != attempt
            or p.get("source_sha") != source
            or re.fullmatch(r"[0-9a-f]{40}", str(p.get("app_target_sha", ""))) is None
            or p.get("diagnostic_only") is not True
            or any(p.get(k) is not False for k in ("matrix_credit", "slo_credit", "diagnostic_credit"))):
        raise SystemExit("fingerprint identity or credit flags are invalid")
    result = {}
    phase = p.get("capacity_phase")
    if isinstance(phase, dict):
        result["capacity_phase"] = {
            k: phase[k] for k in ("resource_abort_reason", "control_failure_reason") if k in phase
        }
    observer = p.get("memory_observer_failure")
    if isinstance(observer, dict):
        result["memory_observer_failure"] = {
            k: observer.get(k) for k in ("stage", "error_class", "remote_stage", "remote_reason")
        }
    print(json.dumps(result, sort_keys=True))
else:
    required = {
        "schema", "event", "workload", "source_sha", "app_target_sha",
        "external_run_id", "external_run_attempt", "status", "stage", "reason",
        "section", "field", "present", "value_type", "binding_status",
        "matrix_credit", "slo_credit", "diagnostic_credit",
    }
    optional = {"projection_reason", "missing_known_field_count", "unexpected_field_count"}
    if not isinstance(p, dict) or not required <= set(p) <= required | optional:
        raise SystemExit("sanitizer artifact has an unexpected schema")
    if (type(p.get("schema")) is not int or p["schema"] != 1
            or p["event"] != "ssr_capacity_sanitizer_rejection"
            or p["workload"] != "authenticated_html_ssr_capacity_120s_plus_cpu_profile_20s_v1"
            or p["source_sha"] != source
            or re.fullmatch(r"[0-9a-f]{40}", str(p["app_target_sha"])) is None
            or p["external_run_id"] != run_id or p["external_run_attempt"] != attempt
            or p["status"] != "invalid"
            or any(p[k] is not False for k in ("matrix_credit", "slo_credit", "diagnostic_credit"))):
        raise SystemExit("sanitizer rejection identity or credit flags are invalid")
    if p.get("projection_reason") == "field_set_mismatch" and (
            type(p.get("missing_known_field_count")) is not int
            or type(p.get("unexpected_field_count")) is not int):
        raise SystemExit("sanitizer projection counts are invalid")
    print("closed sanitizer rejection; all credit flags are false")
PY
~~~

Keep this private evidence with the exact run; do not paste JSON into a ticket or job log.

CPU-only and SSR-capacity fixture confirmations are accepted by the trusted
origin supervisor only for the fixed one-tournament, fourteen-user fixture
with timeout diagnostics disabled. The confirmation check runs after the
run-bound export directory and its private `supervisor.exit` trap are created,
but before fixture preparation. The runner polls the existing `ready` and
manifest handoff first, then accepts only a bounded canonical exit status
(`0`–`255`) from the exact run export. A nonzero status can authorize the
separate exact cleanup attempt only when the manifest is absent and both the
load-client and CPU/SSR candidate jobs are actually skipped. It never marks
namespace closure or the manual barrier successful; the overall run remains
failed, and the three-hour origin finalization poll is skipped without a
manifest. Cleanup still uses the ordinary run-bound identity and control
account-preservation checks. A missing, malformed or zero status without a
manifest remains a manual-barrier failure.

For a CPU-only or SSR-only diagnostic, the finalizer also permits exact cleanup
after successful setup when both candidate jobs were skipped. Before SSH it
validates the encrypted input, descriptor/source binding and run identity. If a
candidate job ran, this route is unavailable; cleanup never proves namespace
closure or grants phase credit, and the diagnostic remains failed.

A structurally complete budget miss may pass through origin attachment and
sanitization so its evidence is retained, but final workflow enforcement still
fails the run. A status `0` or `500` result may use the same reserved candidate
handoff only when all raw, logical and phase status counts, error-class totals,
source/run/profile bindings, timing, containment and population evidence close
exactly. Its `contract_ok` and `passed` fields remain false; origin attachment
and sanitization retain the failed measurement, and final workflow enforcement
still fails the run. Unknown statuses or error classes, extra failed checks,
incomplete counts, partial work, observer failures, cleanup failures or
artifact-identity mismatches remain invalid pipeline results and cannot be
reclassified as completed failures.

The optional CPU diagnostic pair removes its run-bound SSH files before the
candidate report sanitizer runs. Cleanup runs even after a pair failure; a
sanitized failure summary may be retained when cleanup succeeds. If cleanup
fails, the raw report stays private, sanitization is skipped, no summary is
uploaded, and the run fails.

The QA system sampler keeps full process identity and CPU collection for all
`/proc` rows while reading per-process RSS and I/O counters only for the seven
groups it reports. The generic process iterator still collects the full
record by default. A synthetic 189-process `/proc` fixture over ten iterations
reduced RSS/I/O file reads from 3,780 to 140 and local fixture wall time from
0.760 s to 0.374 s. This is a fixture benchmark, not a production CPU or
under-load measurement; it does not establish a production performance gain.
The change remains a measured optimization hypothesis until the R load matrix
provides comparable observer evidence.

Artifact handoffs carry the exact artifact ID across jobs. Each consumer reads authenticated Actions metadata and requires the expected run-derived name (including attempt), artifact ID, unexpired state, workflow run ID and source SHA. Its `sha256:` digest must be 64 lowercase hexadecimal characters and match downloaded ZIP bytes; missing metadata or any mismatch fails closed. Consumers do not depend on cross-job digest outputs, which Actions may suppress when secret-like. Every verifier receives its SHA from that job's declared source identity; the evaluator uses `SOURCE_GIT_SHA` for artifact metadata and the candidate receipt binding.

Fixture input and manifest artifacts are credential-bearing ciphertext; see the [fixture credential transport procedure](fixture-credential-transport.md). Inspect only the closed descriptor and separately sanitized report, fingerprint and cleanup evidence.

The candidate receipt is not an acceptance result. The independent evaluator
validates its exact schema and report digest, then binds the origin observer and
decides the measured profile. A complete result whose only failures are
profile-owned budgets publishes the sanitized measurement artifact and leaves
the overall workflow failed. Missing or mismatched receipts, invalid or
incomplete measurements, observer failures, and remote, projection, sanitizer
or cleanup failures remain hard failures and cannot be hidden by the evaluator.

The measured client is supervised in a mandatory Linux PID namespace. The only
privileged chain is the absolute system path `/usr/bin/sudo -n
/usr/bin/setpriv --pdeathsig SIGKILL -- /usr/bin/unshare --pid --fork
--mount-proc --kill-child=SIGKILL`; no checkout-controlled helper is executed
as root and user-namespace flags are deliberately absent. Inside the namespace
a second absolute `setpriv` immediately drops to the original runner UID/GID,
clears groups, applies `--no-new-privs`, and clears inheritable, ambient and
bounding capabilities before it execs the Python worker as PID 1. A validation
preflight on the pinned `ubuntu-24.04` runner probes that exact non-root,
stdin/stdout contour before any production fixture setup, and the load runner
repeats the probe before candidate execution. The runtime also performs the
same pure preflight before it creates report directories, removes stale paths,
writes config, or creates its worker temporary directory; a root/local
rejection is an in-memory error/exit and cannot mutate the caller's report
path. Probe failure is a closed non-authoritative result and never falls back
to a process group or an uncontained worker. The probe and namespace entry
machine-check real/effective/saved UID/GID, supplementary groups, PID 1/PPID 0,
all capability fields and `NoNewPrivs=1`.

The scenario deadline and whole-runner deadline select one absolute monotonic
wall deadline. A bounded reserve inside that deadline covers wrapper wait,
`TERM`/`KILL`, captured-chain and namespace reaping, child-report read/validation
and atomic publication; it includes a one-second post-watchdog-KILL reap
interval for the hosted sudo monitor to reparent and for PID1/captured-chain
reap, in addition to the configured TERM grace. Every phase checks the
deadline before and after its bounded work; a short authored window fails
closed rather than borrowing time from teardown. A blocked DNS/socket/body read or future is terminated with
`TERM`, then `KILL` after only the remaining grace. The supervisor tracks the
complete wrapper chain and namespace PID by pidfd/start-time, waits for the
wrapper, reaps a zombie namespace PID 1 when necessary, and requires namespace
closure before publishing the final report. The final acceptance gate is
adjacent to atomic publication: a teardown, parser or write overrun always
preserves the primary deadline reason and can never produce `reason=none` or a
success envelope. Every report has mandatory boolean
`namespace_closed`; only the parent may set it true after closure. A malformed
or killed worker is represented by a failed report with partial/in-flight-
unknown flags; the primary timeout/containment reason remains intact even when
the child report is absent. The `always()` fixture finalizer has a separate
fail-visible namespace barrier: cleanup never begins when the field is missing
or false and a manual emergency action is required instead. Candidate report
upload is independent of the client exit code, so a runtime timeout cannot
skip cleanup or leave a background load mutating the fixture while cleanup
begins.

The hosted containment canary itself creates a TERM-ignoring `setsid`/double-
fork/nested descendant tree, records each outer PID and `/proc` start-time, and
proves heartbeat and every captured identity stop after closure. Because a
setuid sudo exec may clear a parent-death signal, the non-root
watchdog keeps the supervisor pidfd outside the privileged chain and
identity-signals the entire captured sudo/setpriv/unshare descendant chain if
that pidfd closes. This is a reclaim guard, not a process-group fallback; the
watchdog wraps sudo in the absolute system `setpriv --pdeathsig SIGKILL`
where the system implementation preserves that signal. PID1 pidfd signalling,
start-time checks and bounded full-chain reap remain mandatory proofs when
setuid sudo clears it; the namespace worker and inner setpriv also arm
`SIGKILL` parent-death handling.
If the watchdog's identity-pinned `pidfd_send_signal` returns `EPERM` during a
parent-alive termination, it may skip only a PIDFD/start-time-pinned process
whose `/proc/<pid>/status` proves effective UID 0 and an exact `Name` of
`sudo`, `setpriv` or `unshare`, with exactly one `NSpid` entry (outer PID
namespace only). The watchdog itself forked and execs only the fixed absolute
system chain above. It continues
signalling the remaining captured identities, and the parent still requires
independent wrapper and PID 1 closure proof before publishing a report that
claims namespace closure or allowing cleanup to proceed. This skip is
disabled on the parent-death path; unreadable, malformed, non-root or
unknown identities remain fail-closed. The bounded
`runtime_supervisor.watchdog_error` enum records either
`pidfd_signal_eperm_trusted_mediator_skipped` or
`pidfd_signal_eperm_unclassified` without exposing PIDs or paths.

Load/QA evidence is a fixed, privacy-bounded set: route classes/templates,
numeric timings/counts/statuses and allowlisted error/backend/wait classes. It
excludes control/operator email values, slugs, raw URLs/queries, request or
diagnostic IDs, user digests, edge IP/location, Cloudflare ray values and raw
PostgreSQL SQL. A control account is represented only by
`control_account_preserved: bool`.

Remote output is redirected to a mode-0600 temporary file, reduced to one
bounded canonical summary, and deleted before upload. External-load,
server-observability and timeout-diagnostics JSON each use an explicit
closed-schema projector after private evaluation; public files carry
`public_projection: true` and `authoritative: false`. Exact fixture/run/process
identities remain private for evaluation/cleanup only; required evidence uses
`if-no-files-found: error`, 14-day maximum retention; missing observer/cleanup
evidence fails. Dispatch inputs use the canonical bounded-ASCII parser and
private mode-0600 JSON stdin; SSH carries a fixed dispatcher mode and no raw
input enters reports or artifacts.

When the PID-namespace worker exits normally after catching an exception, the
parent report retains a closed worker-failure discriminator (stage, exception
class, allowlisted module, and numeric source line). The parent still records
the worker's nonzero exit and partial/in-flight status; namespace closure does
not convert that report into a pass. If the source-bound discriminator is
valid and the existing fixture, observer, namespace, and cleanup gates pass,
the failed report may be published for diagnosis while the workflow remains
failed. Exception text, traceback text, paths, and local values are never part
of the public projection.

The origin observer is a supported integration of the
[`platform_production_external_fixture_qa.sh`](../tools/platform_production_external_fixture_qa.sh)
workflow. That workflow enters the canonical retained-load supervisor before
starting the observer and keeps
`/run/lock/oldsparky-retained-load-matrix.lock` held through observer
completion, export and exit; the observer does not acquire a second lock.
The Actions concurrency group serializes this supported workflow, while the
host lock also covers deployment, cleanup and maintenance callers. Direct
observer invocation is outside the supported workflow integration.

When the diagnostic CPU-profile environment is enabled, worker signals are
sent only through Linux pidfds after the exact UID, parent and procfs
start-time identity checks. If either pidfd API is unavailable or delivery
fails, the observer refuses the signal and records a bounded reason; it still
retains its independent system, journal and database evidence. It never falls
back to numeric-PID signalling.

Profile artifacts are attributed only when their filename contains the exact
worker PID and procfs start-time generation captured for the window, the file
is new or changed after the observer baseline snapshot, and `pstats` can parse
it. Stale files, same-PID files from an earlier observer, wrong-generation
files and partial/unparseable files are excluded. The observer preserves all
caller-owned artifacts for the existing host retention procedure. Public
projection retains only bounded counts and fixed signal-rejection reasons; raw
PIDs, start times, identities and filesystem paths do not cross the artifact
boundary.
