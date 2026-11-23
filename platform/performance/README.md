# Platform performance contract

- Status: Active reference
- Owner: Platform performance
- Last reviewed: 2026-11-23

[`docs/CURRENT.md`](../docs/CURRENT.md) owns the live production disposition
and open performance gates. This reference owns profile contracts and detailed
measurement evidence; dated checkpoints are historical evidence.

The top-level JSON files in `profiles/` are the only authored canonical load
contracts (schema 2). `tools/platform_load.py` validates, fingerprints and
dispatches them; `tools/platform_external_load.py` remains the external HTTP
implementation detail. `tools/platform_load_acceptance.py` owns the shared
SLO, capacity, spike and stress decisions.

## SSR C32 comparison checkpoint (2026-10-29)

The retained comparison contains four accepted rows in the A1/B1/A2/B2
sequence. Together they completed `2,484/2,484` HTTP 200 responses: A1 `616`,
B1 `648`, A2 `600`, and B2 `620`. The B candidate did not meet the required
pairwise improvement threshold, so the comparison does not show a performance
gain. No capacity ceiling can be inferred from these rows.
The final B2 run [40786898526](https://github.com/StrayForest/old_sparky/actions/runs/40786898526)
generated `620` HTML requests in its SSR-capacity diagnostic; its regular
external client job was skipped and exact fixture cleanup passed.

For the accepted 20-second request windows, host CPU samples were filtered to
the report's actual request start/finish timestamps (19/18/19 one-second
samples for A1/B1/A2/B2). Mean CPU use on host CPUs 0/1 was 91.3/90.5% for A1,
93.5/93.5% for B1, 90.4/89.0% for A2, and 91.3/90.1% for B2. Per-core maxima
were 100% for A1/B1/A2 and 99.1% for B2; steal was zero in all four windows.
These short observations show CPU-heavy plateaus, not sustained utilization
or an absolute capacity limit. The API process group plus web process used
about `1.4` CPU cores across the four windows; the API row sums two workers
and retains only minimum/maximum worker durations, so this is approximate and
does not represent total host CPU. PostgreSQL used about `0.11` CPU cores. API
and web were the main measured process CPU consumers.

SSR sampling recorded `638/674/624/652` requests for A1/B1/A2/B2, with
`626/670/624/641` page-stage rows. `workspace_http_fetch` p95 was
`402/395/401/408 ms`; JSON parse was `0.85/1.22/0.98/1.24 ms` and projection
was `0.15/0.12/0.11/0.09 ms`. JSON parsing and projection were small in these
samples. These nested SSR stage times are broad aggregates, not active-window
request percentiles or CPU durations. No SSR auth-bootstrap stage,
root-layout/React tail duration, or function-level profile was captured;
broader API diagnostics record auth-bootstrap separately. API
request-performance recorded `147/161/148/153` samples, with only `66–80`
partial SSR joins; B2's API samples split into `80` workspace and `73`
auth-bootstrap rows. These records are not timestamped to the active request
window. No CPU stacks, event-loop samples, or GC samples were captured. The B
transport selector applies to `/auth/bootstrap`; the tournament workspace
request remains on the shared `platformFetch` path. The evidence does not
justify a workspace transport change or another code-level fix.

The retained A1/B1/A2/B2 PostgreSQL observer artifacts were collected before the
query-identity correction. That collector mapped null or nonnumeric query IDs to
a shared `other` identifier, so distinct statements could collide in their
query-ID-keyed deltas; those historical rows are not per-statement interval
costs and are not retroactively revalidated by this change. The reviewed
candidate collector retains validated numeric `queryid` values, excludes
invalid or ambiguous identities instead of merging them, and marks coverage
incomplete when the top-500 snapshot or unknown identities prevent a complete
pairing.
The public `other` query category is a category label, not an identifier. This
candidate fix has not been deployed; it improves observer correctness but does
not prove an application bottleneck or a specific performance gain.

Private analysis reference: `retained-resource-attribution-20261029.md`,
SHA-256 `612026f8cde27f2a5b977866d6e0ccae442497e15ece36dedc18f645e8a109c9`.
The report remains outside this repository.

The zero-credit production SSR composite is operated through the
[SSR capacity diagnostic runbook](../docs/ssr-capacity-diagnostic-runbook.md).
It defines the source-bound release preconditions, exact dispatch and
completion gates, and abort/cleanup steps. Do not treat an envelope completion
marker as proof that both phases or the fixture cleanup completed.

## Authenticated-page load comparison (2026-10-24)

Candidate [run `40114878581/1`](https://github.com/StrayForest/old_sparky/actions/runs/40114878581), source `70915e23fef5b225b6e272c0aeb50e0d2d253aec`, used the exact `authenticated-page-load-v1` profile from baseline [run `40092960587`](https://github.com/StrayForest/old_sparky/actions/runs/40092960587), source e7. Both used 20,000 users across 40 tournaments, setup concurrency 20, client concurrency 64, zero spread, a 30-second timeout, no retries and the 1,800-second maximum duration.

The candidate completed `20,000/20,000` HTTP requests with status 200 at `36.078` requests/s over `554.35` seconds. HTTP p95/p99 were `2.576/2.937` seconds; full logical p95/p99 were `526.516/549.414` seconds. Its logical SLO **failed** despite every HTTP request returning 200. The baseline duration was `628.28` seconds at `31.833` requests/s; this comparison does not change the failed acceptance result.

Exact cleanup removed all 20,000 synthetic users and 40 tournaments, preserved the control sentinel, and passed namespace/origin-boundary checks. The run earns no passing SLO credit.

The pre-separation v1 contracts and retired saturation sweeps remain under
[`profiles/retained-v1/`](profiles/retained-v1/README.md) solely as historical
semantics for interpreting retained evidence. That directory is outside the
active registry and is not selectable by the production workflow.

## Canonical profiles

| Profile | Class/status | Scenario |
| --- | --- | --- |
| `ready-vote-slo-v2` | default/active | 500-user human-shaped Ready Vote supported-load contract |
| `ready-vote-capacity-ramp-v2` | default/active | 20–80 logical Ready Vote actions/s, 30s steady phases |
| `ready-vote-saturation-ramp-v4` | diagnostic/active | 120–135 logical Ready Vote actions/s, 30s steady phases |
| `ready-vote-transport-pair-v1` | diagnostic/active | On-demand HTTP/1.1 close-versus-keepalive ABBA comparison at 70 actions/s |
| `ready-vote-stress-15k-v2` | default/active | 15,000-user aggressive Ready Vote behavior test |
| `ready-vote-stress-20k-v2` | diagnostic/active | Optional 20,000-user unresolved-question stress test |
| `ready-vote-spike-v1` | default/active | Normal → burst → normal with recovery phases |
| `read-mix-human-v2` | default/active | 500-user human-shaped authenticated reads |
| `read-mix-stress-v2` | default/active | 20,000-user authenticated reads and conditional reloads |
| `read-mix-concurrency-ramp-v1` | diagnostic/active | Full read mix at c16/c32/c48/c64/c80/c96/c112/c128 |
| `authenticated-page-load-v1` | default/active | Full authenticated Next.js tournament HTML control |
| `authenticated-page-load-v2` | diagnostic/active | HTTP/1.1 keep-alive transport comparison |

The current source portfolio contains 12 active profiles, all dispatchable
through the production external-load workflow. Eleven profiles form the final
load matrix; `ready-vote-transport-pair-v1` is a separate on-demand diagnostic
and never counts toward that matrix or its capacity result. It uses four
30-second 70-action/s phases in close/keepalive/keepalive/close order, each on
a distinct five-tournament cohort with 420 actors per tournament (the other
80 fixture users per tournament remain untouched); its timing comparison does
not alter the matrix profiles or their acceptance thresholds. The retired
saturation v1–v3 contracts are retained only under
[`profiles/retained-v1/`](profiles/retained-v1/README.md); lifecycle QA has no
canonical JSON placeholder in this registry and remains a separate
preproduction harness concern. Derive this portfolio from the executable
[profile registry](../tools/platform_load.py)
and the reviewed [production workflow choices](../../.github/workflows/platform-production-external-load.yml),
not from retained report counts.

## Current 11-profile production matrix

The matrix is run serially. Cells 1–8 and 10–11 completed on the previous
P4/C baseline. Cell 9 is `read-mix-concurrency-ramp-v1` run `41937909055/1`
with runner source S and app source A both
`7c6034efe3878c3413090ca1bcdb5be6e93154e8`, and host-tools C
`0de51aca4b4d04a80a2f0d392003cc258d8f1400`; exact cleanup passed. Each of the
eight stages observed 40/40 active Ready Checks and passed the state guard;
minimum deadline headroom was about 2,424 seconds. The full 170,000-action
population succeeded (160,000 HTTP 200 and 10,000 conditional 304), with no
errors, retries or drops. The stress contract failed with
`capacity_ramp_evidence` as its sole failed evaluator check; per-stage logical
p95/p99 budgets failed at all eight stages. Aggregate logical p95/p99 were
209.4/331.2 seconds against 12/20-second limits; HTTP p95/p99 were 886/1,135 ms.
Capacity attribution is unknown
(`runner_window_incomplete`) because all offered ramp tasks started late and
none were dropped. Stage goodput at c16/c32/c48/c64/c80/c96/c112/c128 was
55.3/106.8/137.5/157.6/157.3/157.3/161.0/164.1 actions per second. Origin CPU
was about 99.4–99.8% per core at c64–c128 (API about 1.42 cores, PostgreSQL
0.23–0.24 cores); no lock waiters were observed. This run does not certify a
server capacity ceiling. For each run, record runner S,
application A and host-tools C from exact workflow/deployment receipts; do not
infer one identity from another. The separate corrected SSR
capacity diagnostic is not a matrix cell. Do not switch app or host-tools
profiles while a cell is active.

Run one profile at a time in this order, using the canonical external-load
workflow dispatch and hidden control-account prompt in the
[operations runbook](../docs/operations-runbook.md#live-production-external-load):
change only `profile_id` for each cell; never batch-dispatch the matrix.

1. `ready-vote-slo-v2`
2. `read-mix-human-v2`
3. `ready-vote-spike-v1`
4. `ready-vote-capacity-ramp-v2`
5. `read-mix-stress-v2`
6. `authenticated-page-load-v1`
7. `authenticated-page-load-v2`
8. `ready-vote-saturation-ramp-v4`
9. `read-mix-concurrency-ramp-v1`
10. `ready-vote-stress-15k-v2`
11. `ready-vote-stress-20k-v2` — run only if all prior cells have closed
    cleanly and the operator's current safety/preflight checks still pass;
    otherwise mark it not run and leave the matrix incomplete.

Current disposition (2026-11-05): all 11 cells have run with authoritative
cleanup. Cell 9 passed the active Ready Check state guard in all stages, but
the stress contract failed and capacity attribution remains unknown because
the runner window was incomplete. No matrix rerun is pending. Record runner S,
app A and host-tools C from exact receipts for any future run.

| Cell | Profile | Contract result | Exact cleanup |
| ---: | --- | --- | --- |
| 1 | `ready-vote-slo-v2` | SLO PASS | PASS |
| 2 | `read-mix-human-v2` | SLO PASS | PASS |
| 3 | `ready-vote-spike-v1` | SPIKE BEHAVIOR PASS | PASS |
| 4 | `ready-vote-capacity-ramp-v2` | CAPACITY TARGET PASS through tested 80 actions/s | PASS |
| 5 | `read-mix-stress-v2` | STRESS BEHAVIOR FAIL on whole-cohort logical delay under saturated c128; 30,000/30,000 HTTP requests succeeded, including 10,000 conditional 304s | PASS |
| 6 | `authenticated-page-load-v1` | STRESS BEHAVIOR FAIL on whole-cohort schedule delay; 20,000 HTTP 200 responses | PASS |
| 7 | `authenticated-page-load-v2` | STRESS BEHAVIOR FAIL; 53/20,000 `GET tournament_detail` requests timed out; retained samples show status 0 and about 30 seconds; time clustering and Nginx reconciliation unresolved | PASS |
| 8 | `ready-vote-saturation-ramp-v4` | STRESS BEHAVIOR PASS under contract despite bounded 503 responses and shedding | PASS |
| 9 | `read-mix-concurrency-ramp-v1` | STRESS BEHAVIOR FAIL: state guard passed at all stages (40/40 active, minimum deadline headroom about 2,424 s); 170,000/170,000 HTTP actions succeeded, with zero errors, retries or drops. The sole failed evaluator check was `capacity_ramp_evidence`: per-stage logical p95 and p99 budgets failed at every stage. Aggregate logical p95/p99 were 209.4/331.2 s against 12/20 s; HTTP p95/p99 were 886/1,135 ms. Capacity attribution is unknown (`runner_window_incomplete`): 100% of offered ramp tasks started late, none were dropped; no server ceiling is certified. | PASS |
| 10 | `ready-vote-stress-15k-v2` | STRESS BEHAVIOR FAIL: 2,804/15,000 primary actions and 101/750 same-state duplicates failed; all HTTP errors were 503, with no idempotency state violation | PASS |
| 11 | `ready-vote-stress-20k-v2` | STRESS BEHAVIOR FAIL: 16,520/21,000 logical actions succeeded; 24,224/40,744 HTTP requests returned 503 | PASS |

Cells 5–7 and 9–11 failed their authored acceptance contracts. Cell 9's state
guard passed, but per-stage logical p95 and p99 budgets failed at all stages.
Capacity attribution is unknown because 100% of offered ramp tasks started
late; no server capacity ceiling is certified. Cell 11 showed CPU pressure and
no database lock queue, but the protective-shed trigger remains unproven.

Before every cell dispatch, obtain a fresh canonical FULL readiness receipt
after the automatic production chain has completed and before fixture setup
starts. Use release receipts for A and its previous release, and the active
host-tools pin for C; the verifier SHA must equal A. Run this in the authorized
root shell on the VPS, before any fixture workflow is dispatched. The collector
uses the canonical locks and must not be run while a load, setup, maintenance,
release or cleanup process holds one.

```bash
set -euo pipefail
umask 077
EXPECTED_APP_SHA='<active-app-source-sha-from-release-receipt>'
EXPECTED_PREVIOUS_APP_SHA='<previous-app-source-sha-from-release-receipt>'
EXPECTED_RELEASE_MANIFEST_SHA='<active-release-manifest-sha-from-release-receipt>'
EXPECTED_PREVIOUS_RELEASE_MANIFEST_SHA='<previous-release-manifest-sha-from-release-receipt>'
EXPECTED_HOST_TOOLS_SHA='<active-host-tools-source-sha-from-pin>'
EXPECTED_PREVIOUS_HOST_TOOLS_SHA='<previous-host-tools-source-sha-from-pin>'
VERIFIER_SOURCE_SHA="$EXPECTED_APP_SHA"
EVIDENCE_DIR="$(mktemp -d /root/platform-full-readiness.XXXXXX)"
test "$(/usr/bin/stat -c '%a' "$EVIDENCE_DIR")" = 700
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
/usr/bin/python3.12 -I -B - \
  "$EVIDENCE_DIR/readiness.json" "$VERIFIER_SOURCE_SHA" "$EXPECTED_APP_SHA" \
  "$EXPECTED_PREVIOUS_APP_SHA" "$EXPECTED_RELEASE_MANIFEST_SHA" \
  "$EXPECTED_PREVIOUS_RELEASE_MANIFEST_SHA" "$EXPECTED_HOST_TOOLS_SHA" \
  "$EXPECTED_PREVIOUS_HOST_TOOLS_SHA" <<'PY'
import hashlib, json, sys
from pathlib import Path

path = Path(sys.argv[1])
raw = path.read_bytes()
data = json.loads(raw)
assert len(data) == 23 and data.get("schema") == 1
assert data.get("status") == "ready"
assert data.get("purpose") == "read_only_readiness"
assert data.get("runtime_or_fixture_mutations_authorized") is False
assert data.get("verifier_source_sha") == sys.argv[2] == data.get("app_target_sha") == sys.argv[3]
assert data.get("current", {}).get("source_sha") == sys.argv[3]
assert data.get("current", {}).get("release_json_sha256") == sys.argv[5]
assert data.get("previous", {}).get("source_sha") == sys.argv[4]
assert data.get("previous", {}).get("release_json_sha256") == sys.argv[6]
host = data.get("host_tools", {})
assert host.get("source_sha") == sys.argv[7]
assert host.get("expected_previous_generation_sha") == sys.argv[8]
print("status=ready fields=23")
print("readiness_sha256=" + hashlib.sha256(raw).hexdigest())
print("evidence_path=" + str(path))
PY
```

Keep the receipt and its printed SHA in the private operator checkpoint. Only
after this command exits successfully, dispatch the selected profile from the
authenticated operator shell using the existing hidden-input procedure in the
[operations runbook](../docs/operations-runbook.md#live-production-external-load).
The installed collector owns the pinned closure, service health, lock,
marker/plan/job, maintenance, release-origin and final-consistency checks; its
`status=ready` is required in addition to the exact-source receipt assertion.
The checkpoint must bind the receipt to the same A, previous A, release
manifest digests, C and previous C used for the dispatch. A child smoke,
fixture-preparation check, or readiness check started after setup has acquired
the canonical lock is not a FULL readiness pass. Stop if the collector or any
receipt assertion fails. Do not change the runner source, active A, host-tools
C or selected runtime profile during the sequence. These are workload-profile
dispatches, not permission to apply a runtime configuration.

For every cell, dispatch one run with that exact `profile_id`; record run ID,
attempt, runner `SOURCE_GIT_SHA`, active `APP_TARGET_SHA`, host-tools C,
profile version/digest and the sanitized artifact identities. Keep fixture
setup evidence separate from measurement: setup's API registrations,
fixture-created user/tournament counts and `setup_status` are preparation
activity, not measured client HTTP requests, logical actions, rate or
throughput. Origin observer traffic is also not part of the client request
denominator. The profile's canonical JSON owns its fixture size, traffic,
expected statuses and pass/fail limits; do not override them or compare a
complete stress behavior result to an ordinary-traffic SLO.
Fixture size is profile-specific and can reach 40 tournaments × 500 users
(20,000 users); do not assume every cell uses that maximum.
For `read-mix-concurrency-ramp-v1`, the fixture uses the public scheduled
tournament workflow: registration closes at +4 minutes, Ready Check starts at
+5 minutes, and its 120-minute window is profile-bound. The normal automation
worker creates each round; the fixture waits at most 360 seconds for all 40 to
become active and fails if the minimum deadline remaining is at most 65
minutes. The external runner then validates every concurrency stage against
the workspace responses already in that stage's read mix: each stage must
observe an active round for all 40 tournaments, and every response must leave
more time than the load runtime's remaining budget plus a five-minute startup
margin. Missing, closed, stale-clock, or near-expiry state invalidates that
stage. Public evidence contains only cohort counts, minimum remaining time,
and minimum headroom over the per-response requirement; identifiers and
response bodies are not retained. The runner captures request start/end clock
bounds for these workspace reads independently of timeout-diagnostic IDs; it
does not enable timeout diagnostics or add diagnostic headers. Scheduled
tournament creation opens registration when its start time is due or omitted,
so the fixture uses that created state and skips the otherwise-required
open-status PATCH. Other profiles keep their existing manual Ready Check
setup.
The private fixture timing receipt keeps monotonic stage durations and UTC
start/end bounds. Its safe matrix projection also includes aggregate status,
error, latency and response-byte metrics for tournament create, join and
registration-close API requests; these setup requests remain outside the
external runner's measured HTTP population.

The targeted `authenticated-page-load-v2` diagnostic repeat is complete for
the original 53-timeout question; its truncated sample and unavailable
client-to-Nginx join do not identify a cause. Do not repeat the full
11-profile matrix for this diagnosis. The external-load
matrix remains distinct from whole-site functional QA and does not establish
registration, profile, tournament administration or complete lifecycle
coverage.

Accept a cell only after the exact-source workflow and external client finish,
the client report validates against that profile's schema and acceptance
contract, and the run-bound finalizer passes exact cleanup, namespace/origin
boundary and control-account preservation checks. Use the canonical public
client projection `external-client/external-load.json` from the exact
`platform-production-external-load-<run_id>-<attempt>` artifact directly; it
is marked `public_projection=true` and `authoritative=false`. When present,
preserve its numeric `phases.ramp.phases` rows for per-step counts, offered
rates, latency and window evidence. Select needed numeric fields
programmatically without dumping per-request IDs or private inputs; no second
sanitizer/projection is required. The origin `server-observability.json` and
exact cleanup receipt are required alongside it. The
`external-evidence/matrix-summary.json` artifact is fixture-provisioning/setup
evidence only; it is not the HTTP workload result.
`request_perf` rows are a selected server-side diagnostic sample, not the
client request population. Align host/CPU observations to measured phase
bounds and use the public client report as the request denominator. The exact
workflow evaluator and cleanup receipt own acceptance; the public projection
does not. Record the result as PASS, FAIL, INVALID or NOT RUN; a complete run
may still fail its target.
Do not dispatch the next cell until cleanup is terminal and verified. On a
cancelled, incomplete or ambiguous run, keep the sequence stopped and follow
the same-run abort and cleanup procedure in the
[retained-load runbook](../docs/retained-load-and-web-verification.md). Never
reuse a fixture, infer cleanup from an envelope marker, or publish the
temporary fixture manifest.

This matrix measures Ready Vote request behavior, authenticated read routes,
and authenticated HTML HTTP responses under the versioned profiles. It does
not perform real account signup, email verification, interactive browser UI
testing, or the full tournament lifecycle. The 10,000-user tournament
lifecycle QA contour described in the operations runbook is QA/preproduction
only and must never be pointed at production; no configured QA target is part
of this production matrix. Therefore these load results are not a claim of
whole-site functional coverage. Keep browser/live-user QA and the separate
QA-only lifecycle acceptance as separate gates.

Each profile owns its versioned fixture shape, setup concurrency, offered
logical actions, HTTP concurrency, spread, timeout, retry policy, expected
statuses, correctness requirements, latency/failure/resource budgets and exact
cleanup contour. The portfolio block additionally records owner, hypothesis,
class/status, cadence, environment, request/cost budget and last accepted
evidence. Only active profiles are runnable. Its SHA-256 digest is recorded
with every retained result.

## Host tmpfs allocation diagnosis

The active operator procedure has moved to [Host tmpfs allocation diagnosis](../docs/host-tmpfs-allocation-diagnostic.md), which owns its source, limits, inputs, output validation and abort contract.

## External-load execution safety

The active runner and evidence contract is owned by [External-load execution safety and evidence](../docs/performance-external-load-safety.md).

### Runtime deadlines and kill boundary

See [the active contract](../docs/performance-external-load-safety.md#runtime-deadlines-and-kill-boundary).

### Fail-closed timing and observer evidence

See [the active contract](../docs/performance-external-load-safety.md#fail-closed-timing-and-observer-evidence).

### External-load memory and diagnostic bounds

See [the active contract](../docs/performance-external-load-safety.md#external-load-memory-and-diagnostic-bounds).

### Evidence privacy boundary

See [the active contract](../docs/performance-external-load-safety.md#evidence-privacy-boundary).

## External authenticated-page and CPU diagnostic checkpoint (2026-10-18)

Historical details: [External authenticated-page and CPU diagnostic checkpoint (2026-10-18)](../docs/archive/performance-checkpoints-2026-10.md#external-authenticated-page-and-cpu-diagnostic-checkpoint-2026-10-18).

## Performance and backup checkpoint (2026-10-14)

Historical details: [Performance and backup checkpoint (2026-10-14)](../docs/archive/performance-checkpoints-2026-10.md#performance-and-backup-checkpoint-2026-10-14).

## Backup and restore follow-up (2026-10-15)

Historical details: [Backup and restore follow-up (2026-10-15)](../docs/archive/performance-checkpoints-2026-10.md#backup-and-restore-follow-up-2026-10-15).

## Historical production performance stage (2026-09-07)

Historical details: [Historical production performance stage (2026-09-07)](../docs/archive/performance-checkpoints-2026-09-07.md#historical-production-performance-stage-2026-09-07).

## Read-path candidate slice (2026-09-03)

Historical details: [Read-path candidate slice (2026-09-03)](../docs/archive/performance-checkpoints-2026-09-03.md#read-path-candidate-slice-2026-09-03).

## Ready Vote evidence (2026-08-30/31)

Historical details: [Ready Vote evidence (2026-08-30/31)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#ready-vote-evidence-2026-08-3031).

## Ready Vote saturation after the final FastAPI fast path (2026-08-31)

Historical details: [Ready Vote saturation after the final FastAPI fast path (2026-08-31)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#ready-vote-saturation-after-the-final-fastapi-fast-path-2026-08-31).

## Read-mix stress evidence (2026-09-01)

Historical details: [Read-mix stress evidence (2026-09-01)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#read-mix-stress-evidence-2026-09-01).

## Read-mix stress baseline (2026-09-02)

Historical details: [Read-mix stress baseline (2026-09-02)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#read-mix-stress-baseline-2026-09-02).

## Read-mix A/B after authenticated-read optimizations (2026-09-02)

Historical details: [Read-mix A/B after authenticated-read optimizations (2026-09-02)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#read-mix-ab-after-authenticated-read-optimizations-2026-09-02).

## Read-mix A/B after duplicate avatar lookup removal (2026-09-02)

Historical details: [Read-mix A/B after duplicate avatar lookup removal (2026-09-02)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#read-mix-ab-after-duplicate-avatar-lookup-removal-2026-09-02).

## Read-mix validation after conditional-detail fast path (2026-09-02)

Historical details: [Read-mix validation after conditional-detail fast path (2026-09-02)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#read-mix-validation-after-conditional-detail-fast-path-2026-09-02).

## Read-mix A/B after diagnostic-log sampling (2026-09-02)

Historical details: [Read-mix A/B after diagnostic-log sampling (2026-09-02)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#read-mix-ab-after-diagnostic-log-sampling-2026-09-02).

## Read-mix validation after rollback (historical checkpoint, 2026-09-02)

Historical details: [Read-mix validation after rollback (historical checkpoint, 2026-09-02)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#read-mix-validation-after-rollback-historical-checkpoint-2026-09-02).

## Read-mix A/B after combined workspace base/access preflight (2026-09-02)

Historical details: [Read-mix A/B after combined workspace base/access preflight (2026-09-02)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#read-mix-ab-after-combined-workspace-baseaccess-preflight-2026-09-02).

## Read-mix A/B after combined workspace base/access/Ready Check preflight (2026-09-02)

Historical details: [Read-mix A/B after combined workspace base/access/Ready Check preflight (2026-09-02)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#read-mix-ab-after-combined-workspace-baseaccessready-check-preflight-2026-09-02).

## Rejected conditional workspace auth preflight (2026-09-02)

Historical details: [Rejected conditional workspace auth preflight (2026-09-02)](../docs/archive/performance-checkpoints-2026-08-30-to-2026-09-02.md#rejected-conditional-workspace-auth-preflight-2026-09-02).

## Tournament lifecycle read models

The lifecycle QA mode uses the existing API flow and stores only serialized,
revisioned tournament representations in the shared Redis instance:

- `teams` — normalized `detail.teams` state;
- `workspace_detail` — the normalized team projection embedded in detail
  workspace responses;
- `bracket_summary` and `bracket_full` — the existing bracket response shapes.

PostgreSQL remains authoritative. Successful team/bracket mutations rebuild
only the affected projections after commit. A Redis hit is served without a
representation-building PostgreSQL query; a miss or Redis outage builds from
PostgreSQL and returns the normal response. Redis writes use a revision-aware
atomic compare-and-set and a long safety TTL; TTL is not a consistency
mechanism. `request_perf` and the existing QA HTTP recorder expose hit/miss,
build/write/error/fallback, revision, build/get/set timing and payload bytes.

The lifecycle harness reports `teammate_profile_reads` and
`opponent_profile_reads` separately, uses the real detail/bracket workspace
queries, and checks both `200` and conditional `304` bracket refreshes.

```bash
cd platform
.venv_platform/bin/python tools/platform_load.py list
.venv_platform/bin/python tools/platform_load.py validate
```

The production workflow accepts only a reviewed profile ID. An operator
override is a non-canonical experiment and must retain the changed values and
must not be compared directly with canonical evidence.
