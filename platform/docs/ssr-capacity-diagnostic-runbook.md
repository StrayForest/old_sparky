# Production SSR capacity diagnostic

- Status: Active operator procedure
- Owner: Production operator
- Last reviewed: 2026-11-04

Use this runbook for the zero-credit authenticated SSR composite diagnostic.
It runs one unprofiled 120-second capacity window, then a separate profiled
20-second attribution window against the same fixture of one tournament and
14 users. The general `authenticated-page-load-v1` profile remains 40
tournaments by 500 users; the trusted diagnostic serializer validates that
profile but fixes the diagnostic fixture to its 1-by-14 cohort. The diagnostic
does not change runtime settings and does not earn SLO, capacity, or release
acceptance credit.

## Release and host readiness

Run only after the source fix and its operational instructions pass the full
exact-head CI route. For a runtime-sensitive source change:

1. Merge the reviewed change through the normal `dev` route. Record the exact
   merged source SHA `S` and its successful Security run and attempt.
2. Follow the exact-source AUTO route selected by the validated classifier.
   For a deployable production change, start exactly one source-bound TMPFS
   backup for `S` and its Security run/attempt; it may start during Security or
   after success, and must start within AUTO's bounded proof wait. Require the
   receipt to bind `S` and prove create, restore, Alembic and scratch cleanup.
   A non-deployable recovery-bootstrap change uses automatic baseline
   reconciliation and its verified no-op receipt; it does not require a
   backup. Do not infer that route from the file list or manually dispatch a
   different workflow.
3. Let the normal auto-deploy parent verify its signed host-tools bundle and
   attestation. Install a new host-tools generation only when that exact
   source-bound route requires one, and only from its trusted parent artifact.
   A pull-request candidate artifact is never production authority. Keep
   runner source `S`, active app source `A`, and host-tools generation `C`
   distinct in every receipt; confirm the active `C` pin even when the
   recovery-bootstrap route reuses the installed generation.
   Verify host-tool modes against the installed generation's normalized
   manifest (`0555` executable members, `0444` data), not checkout source
   modes. Runner-only checks requiring `RUNNER_TEMP` or an SSH handoff stay in
   their owning workflow job, not the host shell. See the
   [host-tools provisioning ADR](adr/production-host-tools-provisioning.md).
4. If classifier evidence marks `S` deployable, let the normal deployment
   child install the app. Require strict shared-venv reuse, the exact `S`
   release marker, the expected previous-release pointer, healthy
   `deadlock-api`, `deadlock-web`, and `deadlock-worker`, and no pending
   operation or diagnostic lock. Do not manually install or dispatch a normal
   production deployment. For a non-deployable recovery-bootstrap change, keep
   app source `A` active and require the automatic baseline-reconcile child to
   publish its exact verified no-op source-binding receipt for runner `S` and
   app `A`; do not substitute a manually dispatched deploy or an unverified
   no-op.
5. Confirm the active app is healthy and the source binding is exact. For a
   deployed run, `S` must equal active app source `A`; for the verified no-op
   route, preserve distinct `S` and `A` and require the exact source-bound
   receipt. Run these read-only checks from the authorized host session; pass
   the expected active app SHA `A` as a public value. They print only selected
   systemd fields, release identity and allowlisted runtime settings. The
   preflight filters the process environment to the listed keys, emits only
   those allowlisted values, and never opens or prints the shared environment
   file:

   ```bash
   set -euo pipefail
   systemctl is-active deadlock-api.service deadlock-web.service deadlock-worker.service
   curl --fail --silent --show-error --max-time 5 --output /dev/null http://127.0.0.1:8010/api/v1/health/ready
   curl --fail --silent --show-error --max-time 5 --output /dev/null http://127.0.0.1:3000/
   /usr/bin/python3 -I -B - '<expected-app-sha>' <<'PY'
   import json
   import re
   import subprocess
   import sys
   from pathlib import Path

   expected_sha = sys.argv[1]
   if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
       raise SystemExit("expected app SHA is invalid")
   release = json.loads(Path("/opt/oldsparky/platform/current/RELEASE.json").read_text(encoding="ascii"))
   if release.get("source_git_commit") != expected_sha:
       raise SystemExit("active release source differs from expected app SHA")

   properties = "ActiveState,MainPID,ControlGroup,User,Group,InvocationID,NRestarts"
   units = {
       "deadlock-api.service": ("oldsparky-api", "oldsparky-api"),
       "deadlock-web.service": ("oldsparky-web", "oldsparky-web"),
       "deadlock-worker.service": ("oldsparky-worker", "oldsparky-worker"),
   }
   selected_env = {
       "deadlock-api.service": {
           "PLATFORM_API_WORKERS": "2",
           "PLATFORM_AUTHENTICATED_READ_ADMISSION_ENABLED": "true",
           "PLATFORM_AUTHENTICATED_READ_ADMISSION_CONCURRENCY": "24",
           "PLATFORM_AUTHENTICATED_READ_ADMISSION_MAX_WAITERS": "8",
           "PLATFORM_AUTHENTICATED_READ_ADMISSION_WAIT_TIMEOUT_MS": "250",
       },
       "deadlock-web.service": {
           "PLATFORM_WEB_WORKERS": "1",
           "PLATFORM_WEB_SERVER_AUTH_TRANSPORT": "fetch",
       },
   }
   web_defaults = {
       "PLATFORM_WEB_WORKERS": "1",  # platform_run_web.sh
       "PLATFORM_WEB_SERVER_AUTH_TRANSPORT": "fetch",  # server-auth.ts
   }
   web_allowed = {
       "PLATFORM_WEB_WORKERS": {"1", "2"},
       "PLATFORM_WEB_SERVER_AUTH_TRANSPORT": {"fetch", "node"},
   }
   result = {"release_slug": release.get("release_slug"), "source_git_commit": expected_sha}
   for unit, (expected_user, expected_group) in units.items():
       raw = subprocess.check_output(
           ["systemctl", "show", unit, *(f"--property={name}" for name in properties.split(","))],
           text=True,
       )
       fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
       pid = int(fields.get("MainPID", "0"))
       if (fields.get("ActiveState") != "active" or pid <= 0
               or fields.get("User") != expected_user or fields.get("Group") != expected_group):
           raise SystemExit(f"service identity or state is invalid: {unit}")
       row = {name: fields.get(name, "") for name in properties.split(",")}
       expected = selected_env.get(unit)
       if expected is not None:
           environment = {}
           for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
               key, separator, value = item.partition(b"=")
               if separator and key.decode("ascii", "strict") in expected:
                   name = key.decode("ascii", "strict")
                   if name in environment:
                       raise SystemExit("duplicate allowlisted runtime key")
                   environment[name] = value.decode("ascii", "strict")
           effective = dict(environment)
           value_sources = {name: "environment" for name in environment}
           if unit == "deadlock-web.service":
               for name, default in web_defaults.items():
                   if name not in environment:
                       effective[name] = default
                       value_sources[name] = "launcher_default"
                   elif environment[name] not in web_allowed[name]:
                       raise SystemExit(f"unsupported explicit runtime value: {name}")
           if effective != expected:
               print(json.dumps({
                   "unit": unit,
                   "expected_allowlisted_runtime": expected,
                   "observed_allowlisted_runtime": environment,
                   "effective_allowlisted_runtime": effective,
                   "value_sources": value_sources,
               }, sort_keys=True, separators=(",", ":")))
               raise SystemExit(f"runtime profile values differ: {unit}")
           row["allowlisted_runtime"] = {
               "observed": environment,
               "effective": effective,
               "value_sources": value_sources,
           }
       result[unit] = row
   print(json.dumps(result, sort_keys=True, separators=(",", ":")))
   PY
   ```

   Record `runtime_profile` from the exact successful deployment receipt; the
   live checks above confirm its effective API and web values. Missing web
   environment keys use only the exact S2 launcher/application defaults shown
   in the script and are labeled `launcher_default`; unsupported explicit
   values fail. The check emits only allowlisted process-environment values;
   it never opens a shared environment file or prints other environment data.
   Record each
   `NRestarts` value and compare it after the diagnostic and cleanup; any
   increase invalidates the measurement. Stop if a source, service identity,
   profile value or readiness check differs.

For a complete read-only readiness record, prefer the collector included in
the active app release. Its release-specific pin check validates the flattened
`contracts/host_tools_pin.json` and `tools/` closure against the expected C
generation; the source-checkout pin resolver remains for CI checkout layout.
In the authorized root host shell, set source values from verified release
receipts and the exact host-tools pin, then capture private output:

```bash
set -euo pipefail
umask 077
EXPECTED_APP_SHA='<active-app-sha-A>'
EXPECTED_PREVIOUS_APP_SHA='<previous-app-sha>'
EXPECTED_RELEASE_MANIFEST_SHA='<active-release-manifest-sha-from-deploy-receipt>'
EXPECTED_PREVIOUS_RELEASE_MANIFEST_SHA='<previous-release-manifest-sha>'
EXPECTED_HOST_TOOLS_SHA='<host-tools-sha-C-from-active-pin>'
EXPECTED_PREVIOUS_HOST_TOOLS_SHA='<preserved-host-tools-sha>'
VERIFIER_SOURCE_SHA="$EXPECTED_APP_SHA"
EVIDENCE_DIR="$(mktemp -d /root/platform-readiness.XXXXXX)"
test "$(/usr/bin/stat -c '%a' "$EVIDENCE_DIR")" = 700
test -f /opt/oldsparky/platform/current/tools/platform_readonly_live_readiness.py
/usr/bin/python3.12 -I -B \
  /opt/oldsparky/platform/current/tools/platform_readonly_live_readiness.py \
  --verifier-source-sha "$VERIFIER_SOURCE_SHA" \
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

If the installed collector cannot complete because its own validation path is
the failing component, a separately reviewed read-only verifier may be staged
from its exact source commit. Follow the [private staging procedure](read-only-production-readiness.md).
This alternate establishes readiness only; it never authorizes runtime or
fixture mutations.

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

The source-bound backup and release admission rules are in the
[backup and restore runbook](backup-restore-runbook.md) and
[deployment runbook](deployment-runbook.md). Never use a PR artifact to fill a
production source, backup, host-tools or release identity.

## Dispatch one diagnostic

Confirm `dev` points to the merged source SHA and that no load, maintenance,
release, abort, or cleanup operation is active. Dispatch the external-load
workflow with all workflow inputs represented as strings. The control account
is read by a hidden prompt and sent only in the JSON request body on stdin:

```bash
set -euo pipefail
python3 - <<'PY' | gh api --method POST \
  repos/StrayForest/old_sparky/actions/workflows/platform-production-external-load.yml/dispatches \
  --input -
import getpass
import json
import sys

payload = {
    "ref": "dev",
    "inputs": {
        "confirmation": "RUN-PRODUCTION-SSR-CAPACITY-DIAGNOSTIC",
        "control_email": getpass.getpass("Control account email: "),
        "profile_id": "authenticated-page-load-v1",
        "timeout_diagnostics": "false",
        "cpu_diagnostic_pair": "false",
        "cpu_diagnostic_only": "false",
        "ssr_capacity_diagnostic_only": "true",
        "ssr_capacity_concurrency": "32",
        "ssr_capacity_workload": "authenticated_html_ssr_capacity_120s_plus_cpu_profile_20s_v1",
    },
}
json.dump(payload, sys.stdout, separators=(",", ":"))
sys.stdout.write("\n")
PY
```

The email must not be placed in shell arguments, environment variables, files,
workflow logs, or artifacts. Reconcile the one newly created run by workflow,
event, branch and exact `head_sha`; record its run ID and attempt before
watching it:

```bash
gh api \
  'repos/StrayForest/old_sparky/actions/workflows/platform-production-external-load.yml/runs?event=workflow_dispatch&branch=dev&per_page=10' \
  --jq '.workflow_runs[] | [.id, .run_attempt, .head_sha, .status, (.conclusion // "")] | @tsv'
gh run watch <exact-load-run-id> --repo StrayForest/old_sparky --exit-status
```

Do not dispatch a second run after an uncertain response. Reconcile the run
list and artifacts first. Do not download the private input artifact; review
only the sanitized report, manifest, phase evidence, and cleanup/abort receipts.

## Decide whether the measurement is complete

`report_complete` is an envelope marker, not acceptance. Count a measurement
complete only when the report and canonical validators bind the exact run,
attempt, source, profile, workload, concurrency, fixture manifest and phase
identities, and all of these conditions hold:

- The unprofiled capacity phase completes its full 120-second ON window with
  every submitted request accounted for, successful HTTP status and HTML
  validation, complete required SSR request identity/timing and origin CPU
  evidence, no abort, and successful plan cleanup. Require
  `server_telemetry.request_correlation.status=complete` in the sanitized
  report; an incomplete correlation aggregate cannot receive a PASS even if
  the report envelope says `complete`. In
  `transient_ssr_join_counts`, require `submitted_html_count` to equal the
  started-in-window HTML request count, `valid_response_id_count` to equal
  `submitted_html_count`, and `missing_response_id_count`,
  `duplicate_response_id_count` and `malformed_response_id_count` all to be
  zero. `eligible_response_id_count` is the deterministic one-in-eight join
  sample; `retained_sample_count` may also be limited by its fixed cap. The
  HTTP/1.1 load client's closed response-header projection must retain
  `x-platform-ssr-request-id`; the end-to-end transport contract is covered by
  `test_ssr_response_id_survives_http11_page_capture_and_keeps_closed_validation`.
  Require exactly one raw
  `ssr_stream_capture_coverage` record for the phase run with `status=complete`.
  In the normalized `ssr_stream_coverage` summary require
  `capture_marker_records=1`, `capture_status=complete`, and
  `capture_eligible_requests=capture_retained_requests+capture_lost_requests`;
  `capture_lost_requests`, `capture_lost_rows`, `capture_truncated_records`,
  `capture_malformed_records`, `selector_overflow`,
  `selector_overflow_unattributed`, `row_cap_records`, `row_cap_rows`,
  `truncated_records` and `malformed_records` must all be zero. Missing,
  duplicate, malformed, overflowed or truncated coverage is partial evidence
  even when `report_complete` is true. The web selector releases its reservation
  on that response's `close` event and only when the map still contains that
  exact selection. The same marker reports bounded counts for typed selector
  acceptance/rejection and response-ID header outcomes. Check that selected
  plans equal the sum of header-set, already-sent, unavailable and error
  outcomes. These diagnostic counts do not replace the full-cohort request-ID
  counts or identify an individual request. Verify this contract when changing the guard:

  ```bash
  cd platform/apps/platform_web
  npm run test:ssr-stream-diagnostics
  ```
  Typed HTML trace correlation accepts a proxy-stamped receipt from 28 seconds
  before ON start through the plan's fixed 15-second expiry, never later than
  the current server time. The 28-second floor is the enforced 20-second
  end-to-end plan preparation limit plus the 8-second remote clock-lag bound.
  If a typed HTML plan takes longer than 20 seconds to prepare, the runner
  stops before starting its workload and keeps the prepared plan in the existing
  expiry/cleanup path. The proxy
  strips caller-supplied parent-start values before stamping receipt time; no
  client timestamp is trusted. An active OFF or ON request keeps its existing
  physical phase, CPU/usage/memory/V8 starters and window edges. A request in
  the bounded idle margin is trace-only. The collector reads only that bounded
  interval and joins only exact request IDs selected from the client-started ON
  cohort, so OFF probes, idle requests and other IDs cannot add ON capacity
  credit. Receipt before the floor or after expiry remains uncorrelated. The
  full-cohort ID and completion requirements reject any remaining boundary
  arrival that cannot be joined; do not credit partial evidence.
- The `authenticated_html_ssr_capacity_120s_v1` plan binds the Node memory
  event window to its exact phase duration: OFF remains capped at 20 seconds,
  while ON may span 120 seconds. The final sample endpoint may extend up to
  500 ms beyond the phase boundary under the existing timer tolerance, while
  the sampled interval duration itself remains capped at the planned phase
  length. Legacy and 20-second plan events remain capped at 20 seconds. Require
  the expected five Node samples, valid endpoints within those bounds, and
  complete cgroup evidence where the hierarchy is available. A sample outside
  its exact phase plan, malformed telemetry, or changed service identity makes
  memory attribution incomplete;
  it cannot be accepted by widening the generic parser limit.
- The separate profile phase completes its 20-second ON window against the
  same fixture manifest after the planned gap. Its trace-marked HTML warmups
  prearm API and web OFF/ON usage windows; API also arms a thread-CPU cProfile
  callback on each worker event loop. The callback rechecks the exact plan and
  starts at the planned ON edge; the API rechecks actual start time around
  profiler construction and activation. The monotonic start is captured after
  profiler enable; stop captures the end immediately after disable, before
  plan revalidation or profile summarization. Usage-window CPU and monotonic
  endpoints are likewise snapshotted before plan revalidation. A missing, late,
  replaced, or unreadable plan remains
  incomplete; the first ON request cannot start or shift this profile. Keep the
  250 ms start and end bounds. Web V8 sampling uses a bounded 10 ms interval
  in this opt-in workload, with at most 2,400 samples and 10,000 profile
  nodes. The 120-second capacity workload never enables
  API cProfile. Profile data is attribution only; do not merge it into
  capacity-window latency or CPU results.
- When the API profiler emits more than ten categories, it retains the
  CPU-ranked top nine non-`other` categories (with a category-name tie-break)
  and folds omitted CPU and call counts, including any prior `other` entry,
  into `other`. The server collector sorts the merged category rows by name
  and preserves exact equality between category CPU totals and
  `total_self_cpu_us`. Keep the consumer's conservation check strict; do not
  add a tolerance for category truncation.
- The profile summary carries a closed `profile_rejections` object with a
  capture or summary outcome and bounded per-service rejection-occurrence
  counters.
  Counters classify rejected records, missing/duplicate expected targets, or
  unexpected profile events when the workload does not request profiling;
  they are not a raw journal-row total, and separate predicates may count the
  same failed target. `complete` requires a complete capture and every counter
  to be zero. For an incomplete profile, preserve `profile_status` and
  `profile_reason` and inspect only the closed counters to identify the
  rejecting predicate. The projection contains no journal messages, process
  identities, or profile category values from rejected records. Keep all
  capacity, SLO, matrix and profile performance credit false for this
  diagnostic, including when both phases complete.
- Both phase records, the top-level completion state and the full source and
  fixture bindings pass their closed validators. The phases fit within the
  600-second outer deadline, and finalization and exact fixture cleanup pass.
- No active run-owned process, diagnostic plan, lock, fixture export, or run
  root remains after the canonical cleanup workflow succeeds.

The capacity phase is descriptive and has no latency or throughput threshold.
Any missing phase, request identity, required metric, cleanup proof, binding,
or deadline condition makes the composite incomplete; retain the partial
observations as partial and assign no capacity, SLO, or matrix credit. If
request correlation is the only incomplete capacity evidence and both phase
namespaces and plan cleanups are proven closed, the separate 20-second profile
still runs for diagnosis. Its presence does not complete the composite: full
cohort correlation remains required for `PASS`, and incomplete correlation
receives no credit. Worker failure, resource abort, incomplete non-correlation
telemetry, unclosed namespace, failed plan cleanup, fixture/source mismatch,
quiescence failure, or insufficient wall budget still prevents the profile.
The profiled phase is never capacity evidence. API function self-time is measured
on the event-loop thread and excludes other threads. API profile rows include
at most 20 fixed function IDs with self CPU, cumulative CPU, total and
primitive calls. The self-time coverage field counts only mapped functions;
the category totals still conserve all API self CPU. Cumulative values overlap
their callers and must not be summed into a total. Unknown functions remain in
their existing category and never expose a raw symbol or file path.

Web profile rows contain sampled counts for at most 20 reviewed function IDs
and a mapped-sample count against the total sample count. These are V8 sample
counts, not per-function CPU time. Category values continue to sum sampled
`timeDeltas`; they are diagnostic weights, not process CPU or exact function
self-time. The closed `sample_class_counts` separates idle, garbage-collector,
program, known-category, unmapped-busy and unclassified sample counts; their sum
must equal the total sample count. These counts do not assign CPU time to a
function or category. Unknown frames remain in their existing category.
Neither producer exports raw profile nodes, paths, symbols, request IDs or
source text.

For an incomplete diagnostic, use the one-day failure-fingerprint artifact to
read only the bounded failure classifications. `capacity_phase.resource_abort_reason`
identifies the worker abort class; when it is `control_file_invalid`, use
`capacity_phase.control_failure_reason` only if present. The
`memory_observer_failure` fields classify observer handling and remote-watch
failures. A missing or `unknown` value leaves the cause unresolved. These
classifications do not establish why an underlying file or process failed.
Do not inspect or export raw control files, worker/supervisor reports, or
observer stderr. Any incomplete result remains zero-credit and follows the
exact-run abort and cleanup procedure below.

If the report sanitizer emits `report_projection_rejected`, preserve its
closed `composite_report` field path and `projection_reason` for diagnosis.
These are static schema labels and structural types/counts only; they do not
contain report values or unexpected key names. A rejected report grants no
credit. Do not infer whether either phase ran from missing projected phase IDs;
use independently bound phase evidence and the exact run window.

When a one-day fingerprint or sanitizer-rejection artifact is available, use
the [bounded SSR failure-artifact procedure](retained-load-and-web-verification.md#retrieve-a-bounded-ssr-failure-artifact)
with the exact load run ID, attempt and runner source SHA. It verifies the
authoritative attempt and outer artifact digest before reading the one expected
member, then prints only closed failure classifications. Keep the private
artifact with the exact run evidence; do not paste its JSON into a ticket or
job log.
## Abort and close the exact fixture

If the selected run has an active or uncertain remote process tree, use the
canonical abort workflow for that exact load run and attempt. `target_sha` is
the active app SHA `A`; the abort workflow resolves the runner SHA from the
selected load run. Supply the successful Security run and attempt for the
current `dev` source. The request contains no control-account value:

```bash
gh api --method POST \
  repos/StrayForest/old_sparky/actions/workflows/platform-production-retained-load-abort.yml/dispatches \
  --input - <<'JSON'
{"ref":"dev","inputs":{"confirmation":"ABORT-PRODUCTION-RETAINED-LOAD","load_run_id":"<exact-load-run-id>","load_run_attempt":"<exact-load-attempt>","target_sha":"<active-app-sha-A>","security_run_id":"<successful-security-run-id>","security_run_attempt":"<successful-security-attempt>"}}
JSON
gh api \
  'repos/StrayForest/old_sparky/actions/runs?event=workflow_dispatch&branch=dev&per_page=10' \
  --jq '.workflow_runs[] | select(.path == ".github/workflows/platform-production-retained-load-abort.yml") | [.id, .run_attempt, .head_sha, .status, (.conclusion // "")] | @tsv'
gh run watch <exact-abort-run-id> --repo StrayForest/old_sparky --exit-status
```

Reconcile and review the exact abort receipt. The abort workflow preflight must
validate the successful Security run and attempt for its current `dev` source.
Require the normalized receipt to bind the load run and attempt, runner and
active app sources, fixture input and manifest, and recovery workflow and helper
identity, and to prove the selected process tree absent and shared lock clear. An
abort receipt with `namespace_closed: false` does not prove namespace closure
and grants no measurement credit. The origin supervisor removes both copies of
the session-credential manifest after observer capture. Its root-owned
`diagnostic-abort-binding.json` is written only after validating and hashing
that exact manifest and remains the durable source/run/fixture binding for a
later abort. The abort helper accepts the exact run-bound marker when the
credential-bearing manifest has already been retired. If the marker, its
root-owned fixture directory, or run/source binding is missing or invalid, the
helper fails closed. A missing `supervisor.exit` is handled only by the exact
terminal-failure recovery boundary in the [retained-load cleanup contract](retained-load-and-web-verification.md#retained-load-cleanup-and-hermetic-web-verification);
the schema-2 receipt keeps the absent exit explicit and sets
`namespace_closed: false`. Do not restore or download the private manifest,
create a missing exit record, or hand-write a receipt. Proceed to cleanup only
when the canonical abort normalizer produces
`status=safe_for_exact_fixture_cleanup` for the selected run and attempt.

If the checked-out `dev` source `S` is deployable, differs from the active app
source `A`, and has no exact verified no-op source binding, keep the fixture
closed after abort and do not dispatch cleanup yet. Dispatch exactly one
source-bound backup for `S` and its successful Security run/attempt using the
[backup procedure](backup-restore-runbook.md#local-verified-backup); it may
start after Security succeeds and must start within AUTO's bounded proof
wait. Let the existing AUTO chain complete normal deployment and readiness;
never manually dispatch deployment. A non-deployable recovery-bootstrap `S`
uses the automatic baseline-reconcile route instead: wait for its exact
verified no-op receipt binding runner `S` to the still-active app `A`. This
route needs no app deployment or backup. Do not dispatch a deploy/reconcile
workflow manually or treat an unverified no-op as a source binding.

Before cleanup, verify the successful Security source and cleanup workflow
checkout are the exact 40-character SHA `S`. For a same-source or deployed
binding, verify active `RELEASE.json` also equals `S`; for the verified no-op
route, verify the receipt binds `S` to active `A` and the host helper confirms
`A` against current `RELEASE.json`. In either route, confirm the installed
host-tools pin and normal service readiness checks from
[Release and host readiness](#release-and-host-readiness). The cleanup
source-binding resolver binds its checked-out SHA by same-source identity or
an exact verified no-op receipt; the host helper independently checks the
bound app SHA against current `RELEASE.json`. If `dev` moves, AUTO fails, a
source or receipt binding differs, or readiness cannot be proved, stop and use
the [deployment recovery procedure](deployment-runbook.md#preconditions). Do
not override the app target, create a no-op alias, or dispatch a new workload
before exact cleanup succeeds.

The origin supervisor keeps the observer timeout tree in its own process
session. On `SIGINT` or `SIGTERM`, it stops and reaps that session before it
writes terminal observer progress or `supervisor.exit`. If the exact-run export
lacks a valid supervisor exit record, its process group remains, or observer
progress reports a phase regression, treat the fixture as open: run the
canonical abort for that exact run and attempt, then exact cleanup after the
abort normalizer permits it. Do not create or repair terminal receipts by hand.

Then dispatch exact cleanup. Enter the same control account at the hidden
prompt; the email travels only in the JSON request body on stdin:

```bash
set -euo pipefail
python3 - <<'PY' | gh api --method POST \
  repos/StrayForest/old_sparky/actions/workflows/platform-production-retained-load-cleanup.yml/dispatches \
  --input -
import getpass
import json
import sys

payload = {
    "ref": "dev",
    "inputs": {
        "confirmation": "DELETE-PRODUCTION-RETAINED-LOAD",
        "load_run_id": "<exact-load-run-id>",
        "control_email": getpass.getpass("Control account email: "),
    },
}
json.dump(payload, sys.stdout, separators=(",", ":"))
sys.stdout.write("\n")
PY
gh api \
  'repos/StrayForest/old_sparky/actions/runs?event=workflow_dispatch&branch=dev&per_page=10' \
  --jq '.workflow_runs[] | select(.path == ".github/workflows/platform-production-retained-load-cleanup.yml") | [.id, .run_attempt, .head_sha, .status, (.conclusion // "")] | @tsv'
gh run watch <exact-cleanup-run-id> --repo StrayForest/old_sparky --exit-status
```

Before accepting the cleanup input or reaching SSH, the workflow normalizes the
downloaded `source-binding.json` into its private runner-temp directory. It
accepts only the artifact extractor's owner-owned regular file with one link,
rejects symlinks and unexpected entries, and secures the directory/file as
`0700`/`0600`; the following validator still checks the exact artifact run,
attempt, archive digest and downloaded bytes. A
`CLEANUP_PREFLIGHT reason=source_binding_metadata` or
`SOURCE_BINDING_HANDOFF reason=metadata_invalid` is a pre-SSH failure. Stop and
retain that exact run's evidence; do not edit or substitute the artifact or
bypass validation.

Review the cleanup run's exact source-binding, summary, diagnostic and
canonical-log artifacts. Require `ok=true`, control account preserved, the
expected fixture rows deleted, zero scoped residuals as enforced by the
canonical helper, Redis read-model cleanup, exact run-root/export/marker
removal, successful remote and workflow statuses, and a passed untruncated
canonical log. The helper's zero-residual enforcement is the database and
Redis closure proof; do not substitute manual SQL, Redis commands, filesystem
deletion, or a green job conclusion. If any required artifact is absent,
truncated, mismatched or failed, stop and retain the evidence for exact-run
recovery. Never retry cleanup blindly.

## Owner references

- [Retained-load cleanup and hermetic web verification](retained-load-and-web-verification.md)
- [Performance contract and evidence](../performance/README.md)
- [Abort and cleanup workflows](../../.github/workflows/platform-production-retained-load-abort.yml) and [cleanup workflow](../../.github/workflows/platform-production-retained-load-cleanup.yml)
