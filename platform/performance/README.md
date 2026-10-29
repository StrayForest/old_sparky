# Platform performance contract

- Status: Active reference
- Owner: Platform performance
- Last reviewed: 2026-10-29

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

The PostgreSQL observer uses top-500 `pg_stat_statements` snapshots and maps
nonnumeric or null query IDs to the shared `other` sentinel. Distinct statements
can therefore collide in the query-ID-keyed delta, making those rows
unattributable as per-query interval costs. A future observer correction
should preserve statement identity or mark/drop unmatched deltas instead of
presenting cumulative counters as valid interval values. This is a diagnostic
correctness limitation, not evidence of an application bottleneck; no code
change is proposed at this stage.

Private analysis reference: `retained-resource-attribution-20261029.md`,
SHA-256 `612026f8cde27f2a5b977866d6e0ccae442497e15ece36dedc18f645e8a109c9`.
The report remains outside this repository.

Offline analysis is complete. After implementation and evidence review, the
next authorized measurement is the strict composite contract
`authenticated_html_ssr_capacity_120s_plus_cpu_profile_20s_v1`: the same
authenticated HTML fixture, A/fetch path, API 2/web 1, admission 24 with at most
8 waiters per API worker, C32 concurrency, and 14-actor fixture. Its capacity
plan `authenticated_html_ssr_capacity_120s_v1` runs OFF 20 seconds, GAP 5
seconds, and ON 120 seconds without profiling. After a five-second quiet gap,
the separate `authenticated_html_ssr_cpu_profile_20s_v1` plan runs OFF 20
seconds, GAP 5 seconds, and ON 20 seconds with V8/Python profiling; that
profiled phase earns no capacity credit. The legacy plan's 20-second expiry
remains unchanged; the new plan expires 15 seconds after ON ends. At the web,
API and client boundaries, deterministic request selection hashes ASCII
`run_id:request_id` with SHA-256 and selects one in eight from the low three
bits. Each parent-plan cap is 1,000 requests. Correlation covers the full
submitted ON-start HTTP cohort, including late, failed and drain-completed
requests; phase-scoped API events join one-to-many to requests using private
identifiers, which are not included in public summaries. Measured phases total
195 seconds, plus about 52 seconds of lead per plan, preparation, draining, and
canonical cleanup under a 600-second outer deadline. C64 remains forbidden.
These measurements do not establish a capacity ceiling or a proven application
fix target.
The run remains blocked until the production disk admission requirement in
[current state](../docs/CURRENT.md) is satisfied; no deployment or load run is
currently authorized.

After B2, the production runtime was restored to A. The final deployment was
run [`40788852387`](https://github.com/StrayForest/old_sparky/actions/runs/40788852387).
The last retained status observation, at `2026-10-29T04:28Z`, recorded A
restored with API `2`, web `1`, and authenticated-read admission concurrency
`24` with a maximum of `8` waiters per API worker. It is timestamped, not a
fresh health check. Later storage and maintenance readiness findings are owned
by [current state](../docs/CURRENT.md).

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

Each profile owns its versioned fixture shape, setup concurrency, offered
logical actions, HTTP concurrency, spread, timeout, retry policy, expected
statuses, correctness requirements, latency/failure/resource budgets and exact
cleanup contour. The portfolio block additionally records owner, hypothesis,
class/status, cadence, environment, request/cost budget and last accepted
evidence. Only active profiles are runnable. Its SHA-256 digest is recorded
with every retained result.

## Host tmpfs allocation diagnosis

`tools/platform_tmpfs_allocation_audit.py` is a one-shot, read-only operator
diagnostic for visible inode allocation under the fixed `/dev/shm` root. It
walks metadata twice through no-follow directory descriptors, checks inode and
mount identity, and deduplicates by `(device, inode)`. It writes a bounded
JSONL metadata inventory and one bounded SQLite index inside a newly created
private output directory. SQLite uses an in-memory temp store, a 1 MiB cache,
and a 128 MiB index cap; the scan is bounded to 262,144 entries, depth 32,
64 MiB of inventory output, 16 MiB of encoded names, 90 seconds wall time and
85 CPU seconds. Caps, read failures, identity drift, or incomplete link
metadata produce `UNKNOWN`.

The tool samples only `MemAvailable` and `Shmem` from bounded
`/proc/meminfo` reads before and after the scan. Those values provide time
context; they are not added together, mapped to named files, or used for an
admission decision. `st_blocks * 512` is filesystem allocation for visible
inodes, not resident RAM or a complete tmpfs page count. Deleted-open inodes,
external hardlinks, leases, service references, and process ownership require
separate evidence. Names and metadata can be sensitive, so keep the output
directory private and review it locally. The tool has no delete, cleanup,
backup, service, or deployment behavior and is outside the host-tools bundle
and release payload.

Run only in an explicitly authorized diagnostic window, with an absolute new
output directory whose existing parent chain is root-owned or caller-owned
and not group/other-writable:

```bash
.venv_platform/bin/python tools/platform_tmpfs_allocation_audit.py \
  --output-dir /secure/private-parent/tmpfs-audit-<unique-id>
```

Treat the operation as complete only when the observed process exit is zero,
`terminal.json` is private and readback-valid with
`status=COMPLETE_METADATA`, and no `failure.json` exists. A positive summary
or inventory on its own is not a complete execution receipt. Preserve all
artifacts for review; a name or allocation total never authorizes deletion.

### Runtime deadlines and kill boundary

`portfolio.request_budget.max_duration_seconds` and
`portfolio.cost_budget.max_runner_minutes` select one absolute monotonic wall
deadline. A bounded reserve inside that deadline covers worker startup,
wrapper wait, TERM/KILL, captured-chain and namespace reaping, report
read/validation/publication and the final acceptance gate; no teardown phase
extends the budget. The reserve includes a one-second post-watchdog-KILL reap
interval for the hosted-runner sudo monitor to reparent and for the supervisor
to reap the namespace PID1 and captured chain, in addition to the configured
TERM grace; short profiles therefore fail closed rather than spending their
entire wall budget on measured work. The deadline starts before the trace and
first measured I/O.

`platform_load.py run` is only a supervisor. On the pinned `ubuntu-24.04`
runner it first probes the mandatory Linux PID-namespace contour, then starts
the complete load engine through the absolute system chain
`/usr/bin/sudo -n /usr/bin/setpriv --pdeathsig SIGKILL --
/usr/bin/unshare --pid --fork --mount-proc --kill-child=SIGKILL`. No
checkout-controlled helper runs as root. The inner absolute `setpriv` drops to
the original runner UID/GID, clears groups, sets `--no-new-privs`, and clears
inheritable/ambient/bounding capabilities before it execs the Python worker as
namespace PID 1. The probe and entry check all real/effective/saved UID/GID
values, groups, PID 1/PPID 0, all capability fields and `NoNewPrivs=1`; the
stdio READY/ACK handshake does not depend on inherited descriptors that sudo
could close. A failed probe is non-authoritative and there is no unsafe
process-group fallback; the runtime rejects before mkdir/stale cleanup/config
write/temp-directory creation and returns only an in-memory error/exit on a
root/local runner. The supervisor passes the same absolute deadlines to the
worker and sends `TERM` followed by `KILL` after only the remaining grace.
It tracks wrapper and namespace identities by pidfd/start-time, waits for the
wrapper, reaps a zombie namespace PID 1 when needed, and verifies closure
before publishing the report. Every report has mandatory boolean
`namespace_closed`; only the parent sets it true after closure. A killed,
malformed or incomplete worker produces a failed report with
`partial_work=true` and `inflight_unknown=true`; a missing child report cannot
replace the primary timeout or containment reason. A teardown, report-parser
or publication overrun is always a failed envelope and never `reason=none` or
success. The finalizer blocks all
cleanup on missing/false `namespace_closed` and exposes a separate manual
emergency barrier. Linux namespace workers set `PR_SET_PDEATHSIG=SIGKILL` as
an orphan guard. A non-root pidfd watchdog additionally reclaims the complete
sudo/setpriv/unshare descendant chain when the supervisor disappears, because
a setuid sudo exec may clear the signal across that transition. The hosted
canary uses an eight-second synthetic window with teardown margin, creates a
TERM-ignoring setsid/double-fork/nested descendant tree, records outer
PID/start-time identities and proves heartbeat plus every captured identity
stop after closure. The non-root watchdog wraps sudo in the absolute system
`setpriv --pdeathsig SIGKILL` where the system implementation preserves that
signal; PID1 pidfd signalling and the bounded full-chain reap remain the
authoritative closure proof when setuid sudo clears it.

DNS resolution, TCP connect, TLS, request writes, response headers/body,
retry backoff and executor futures all consume the same absolute budget. The
HTTP/1.1 candidate keeps one connection per load-worker thread and does not
spawn a subprocess per request; a blocked resolver or socket remains
reclaimable because the entire engine is inside the killable PID namespace.

The external-load workflow publishes a closed candidate report whenever one is
available, regardless of the client exit status. Its independent finalizer
still runs with `always()` and owns fixture cleanup; only the later evaluation,
sanitization and exact cleanup gates can produce a successful evidence
artifact.

The fixture producer and load-client consumer share a closed JSON handoff.
`fixture_kind` is required and must be `external_load`,
`cpu_diagnostic_only_v1`, `ssr_capacity_diagnostic_v1`, or
`ssr_html_capacity_diagnostic_v1`; diagnostic kinds are bound to their
confirmation and fixed fixture dimensions. The typed HTML capacity workload
also binds its workload ID and may use only its own 8/16/32/64 concurrency
presets with a fixed 14-actor cohort; the existing workspace capacity profile
uses eight actors and remains limited to concurrency 8/16. Both exercise the
authenticated tournament document over HTTP/1.1 keep-alive, but have different
acceptance semantics and are not comparable as performance A/B arms. SSR
capacity concurrency is present only for those two SSR diagnostics. The consumer also binds
the handoff to the exact target SHA, run ID and fixture source manifest before
starting the candidate.

The SSR diagnostic's exact API/web plan pair expires at the requested
`on_end_ms`; the remote helper refuses prepare while either plan exists and
accepts cleanup only for the plan's embedded run ID after strict expiry. Its
parent waits through that bounded expiry only after the worker namespace is
proven closed and the same invocation's prepare was confirmed. A prepare
refusal under a fresh run ID does not authorize cleanup of an older owner's
plan. The remote helper publishes both service plans as one operation and
attempts to roll back only files whose inode and content hash still match its
own writes.
If a prior plan remains after fixture closure, use the canonical cleanup path
with its embedded owner ID after expiry, then make at most one justified retry;
never remove plan files directly. Plan lifetime is about 105 seconds at most,
with the parent cleanup wait bounded to 107 seconds for the accepted host
clock offset and strict-expiry margin.
For a namespace-open or otherwise incomplete producer result, unavailable
server telemetry uses the existing `unavailable` reason, and an empty
no-sample memory-event set is represented as `null`; neither is converted into
measured zero counters. The parent projection and sanitizer preserve that
closed failure report as `INVALID` with all capacity credits false.

The external runner preserves schema-1 report fields and adds measurement
schema 2. Compatibility `latency` remains service latency; the additive timing
block reports schedule/enqueue/actual start/completion, executor queue wait,
late starts, user-observed latency, actual arrival, goodput and dropped work.
Acceptance requires explicit user-observed latency for every budgeted logical
population; service or accepted-request latency cannot substitute for a
missing/slow schedule-to-completion measurement, so executor saturation
cannot hide coordinated-omission-like delay.

Ready Vote reports always materialize the configured duplicate phase, including
when its count is zero. Stress/spike duplicates are selected only from primary
actions that reached the service; if that leaves fewer candidates than the
configured count, the report retains the configured `expected_count`, records
`missing_actions`, marks the phase incomplete and cannot pass acceptance.
Primary and duplicate logical actions and HTTP attempts are included in the
top-level acceptance aggregates, while the per-phase summaries retain their
individual shedding, retry and completion evidence.

For capacity and spike profiles, the evaluator binds phase names and order to
the selected profile's authored plan, including each phase's configured rate,
duration and logical action count. Missing, extra, reordered or partial phases
fail closed. Ready Vote duplicate evidence is always materialized and must
match configured, submitted, completed and missing action counts exactly;
`complete=false` is never a passing duplicate result. Every non-empty authored
phase, including the duplicate phase, is independently checked against its
accepted-request and user-observed latency, retry-amplification, shedding and
measured successful-goodput budgets. Aggregate percentages cannot hide a
single phase that shed or retried its entire population.

Read-mix and authenticated page-load profiles do not use an authored rate
phase array; their strict plan is derived from the profile contract's named
phase populations (`read_mix`, optional `manual_refresh`, or
`authenticated_page_load`) and exact planned action counts. Their producer
summaries must contain those phases in the same order, with complete logical
and raw timing boundaries. The authoritative `evaluate` boundary applies the
profile dispatchability gate before accepting evidence; retained historical
reports remain inspectable but cannot become authoritative current results.

The evaluator also recomputes every logical and raw-HTTP outcome from strict
integer counters: successes plus failures must equal actions, successful
responses plus errors must equal requests, and the reported failure rate must
match that recomputation. A producer's string `contract_ok`, reported rate,
or total-RPS value is never trusted. Read-mix and authenticated page-load
reports bind their primary population and total logical work to the selected
profile, including every concurrency stage and manual refresh. Ready Vote
reports additionally require one exact raw state-read per planned tournament,
typed authoritative `ready_count` evidence, and a duplicate outcome
distribution with zero state-changing responses. Each phase's raw request
count must reconcile with its logical/timing population; dispatcher-added
runtime HTTP budget fields are allowlisted but revalidated against the
profile during `evaluate`.

The acceptance normalizer treats logical-action and raw-HTTP summaries as
different closed-world envelopes. A logical envelope cannot satisfy a raw
request population (or the reverse), and every canonical envelope carries a
measured successful-goodput value that equals `successful outcomes / measured
wall or completion window` within serialization tolerance. Zero successes can
therefore never claim positive goodput. Raw retry attempts must satisfy
`raw_requests = base_logical_actions + retry_attempts`, match logical
`total_retries`, and stay within the selected retry bound. Raw overload counts,
overload rates and unexpected-status counts are recomputed from the raw error
population before shedding budgets are applied. Phase keys and authored
populations are exact; unknown keys, duplicate JSON object members,
non-finite/negative numeric budgets, and mismatched nested timing populations
fail closed before a PASS can be emitted.

The canonical `platform_load.py evaluate` step accepts only a report bound to
the selected profile and the current external workflow. It requires report
schema/measurement/timing versions, top-level profile ID/version/digest/mode,
the complete profile `load_contract` (including profile schema and production
environment), the checked-out `source_git_sha` (`SOURCE_GIT_SHA`) and the
current `external_run_id` (`GITHUB_RUN_ID`). Runtime accounting fields added by
the dispatcher are explicitly allowlisted; authored contract fields are not
merged or inferred. Missing or mismatched identity is `LOAD REPORT BINDING
FAIL`. Historical schema-1 reports remain retained evidence only and have no
backward acceptance path.

Canonical `run` and `evaluate` require both explicit `SOURCE_GIT_SHA` and
numeric `GITHUB_RUN_ID`; the runner never substitutes the local checkout's
Git HEAD. The standalone `platform_external_load.py` entry point is a
diagnostic implementation detail: without the explicit canonical binding it
is non-authoritative, non-dispatchable and cannot emit an acceptance PASS. It
is not a route into the production workflow.

### Fail-closed timing and observer evidence

Every current request/action timing summary must carry the complete population
boundary: `expected_count`, `submitted_count`, `completed_count`,
`scheduled_count`, `started_count`, `actual_request_start_count`,
`actual_start_count`, `response_completion_count` and `user_observed_count`.
Those counts must agree, `partial` must be exactly `false`, `dropped_work`
must be exactly `0`, and missing/invalid timing-context counters must be zero.
Missing summaries, inconsistent counts, `partial=true`, dropped work or any
missing timing context fail acceptance closed for every current SLO, stress,
spike and capacity report; a percentile from a partial population is not a
passing result. The executable rule is
[`timing_summary_is_complete`](../tools/platform_load_acceptance.py).

The public client report also carries a bounded UTC run window, ordered phase
windows and process-CPU intervals. A CPU interval is publishable only when
its full monotonic-measured interval lies inside the run window; an incomplete,
malformed, or truncated series remains explicitly non-complete. Effective
client CPU capacity remains UNKNOWN unless the report can establish a trusted,
complete view of quota ancestors. A visible cgroup2 mount root, including `/`,
does not prove that namespace-hidden ancestors are absent. The client also records bounded before/after
cgroup diagnostics: cgroup-v2 mount mapping, stable namespace/membership/mount
identity, each visible ancestor's exact quota and period in microseconds, and
nonnegative `cpu.stat` deltas. When an ancestor's quota interface is absent,
the report can retain that ancestor's valid counters with
`cpu_max_kind=unavailable` and an incomplete status. Counter completeness is
explicit: `usage_usec`, `user_usec`, and `system_usec` are required; the
`nr_periods`, `nr_throttled`, and `throttled_usec` group is either present as a
whole or omitted as a whole, never filled with zeros. The closed public
projection contains no paths or namespace, mount or host identifiers. It
distinguishes valid evidence for the visible chain from full quota
completeness. Full quota completeness stays
UNKNOWN because the external runner cannot prove that its visible mount root
matches the trusted host/init cgroup root; even a visible `cpu.max=max` does
not prove an unlimited CPU allocation. Partial mappings, hidden ancestry,
identity changes, missing
interfaces, malformed input, counter resets, or bound exhaustion remain
incomplete or unavailable. These diagnostics do not affect capacity math,
acceptance, load profiles, retry scheduling, or workload timing. Observer
resource timelines follow the same
all-or-nothing rule, with a 4,096-row cap; a present malformed selected metric
invalidates the series, while an absent optional metric remains unavailable.
These fields align client and server observations; they do not imply
whole-window coverage when either report says unavailable or incomplete.

For production external profiles that require exact origin evidence, the
observer binding's `fixture_marker` and `external_run_id` must match the
fixture and external workflow run, and it must report `complete=true` before
acceptance. A missing or empty observer artifact, an observer timeout, or a
marker/run mismatch fails the run; the
client report cannot substitute for the required observer. The observer's API
worker selection is equally fail-closed: it chooses only direct children of
one unambiguous Gunicorn API master, matches the `oldsparky-api` service UID,
and binds each worker to its procfs `start_time_ticks` (and parent identity)
before signalling. Stale/reused PIDs or an ambiguous/restarting tree are not
profiled. See the [observer implementation](../tools/platform_external_load_observer.py)
and the [workflow binding check](../../.github/workflows/platform-production-external-load.yml).

### External-load memory and diagnostic bounds

The external client uses a sliding `FIRST_COMPLETED` window: at most the
configured HTTP concurrency worth of futures/results is live, and the next
action is submitted only after one completion has been reduced. A streaming
phase accumulator keeps counters and compact eight-byte numeric sample arrays;
the historical sorted linear-interpolation percentile algorithm is unchanged.
Its sample population is bounded by the selected profile's planned attempt
count, and the report is rendered only after those accounted samples are
reduced. The former submit-all implementation had a theoretical body-retention
ceiling of about 332 GiB at the 170,000-attempt envelope (170,000 × the 2 MiB
body cap), before parsed-JSON and object overhead. Standard API projection
buffers now retain at most about 8 MiB at concurrency 128 or 32 MiB at
concurrency 512 (64 KiB per live capture), plus an estimated 30 MiB of compact
numeric arrays at the largest accounted sample population. The exact unique
`cf_rays` counter remains bounded by the completed action/attempt count, not by
concurrency, because preserving its report-compatible uniqueness requires
retaining one bounded identity per observed ray. These are static bounds; the
CI-only `ExternalLoadTests.test_real_rss_probe...` acceptance now measures the
two live phases in one fresh Linux child at `C=512, N=4096` with independent
64 KiB synthetic payloads, then a separate child reduces four accumulators at
`N=16384` each with unique `cf_ray` and full timing fields. It reads
`/proc/self/status` (`VmRSS`/`VmHWM`) and `resource.ru_maxrss`, verifies exact
submitted/completed/peak-pending/live-payload counters, native-thread
restoration, direct-child absence and process-group reap, and emits only
bounded JSON. The independent acceptance limits are `VmHWM`/`ru_maxrss` delta
`<192 MiB` for live phases and `<24 MiB` for the accumulator child; live and
hostile probes have absolute 15-second and 0.5-second supervisor deadlines,
respectively, while the accumulator probe has a 30-second deadline. The
existing sliding-window
pending bound remains the mutation contract: the former submit-all
implementation fails when pending work reaches `N`. This is local/CI evidence
only and does not authorize a production load run. Completed request objects,
response bodies and parsed payloads are released immediately. Read/page routes
retain
only status, ETag, byte count, timing and the allowlisted
`AUTHENTICATED_READ_OVERLOADED` code; Ready Vote retains only `code`,
`retryable`, `retry_after_ms`, `changed` and the state-read
`active_round.ready_count` correctness fields.

`error_samples` and `timeout_diagnostics` retain the deterministic first 25
rows per summary. The corresponding `*_total` and `*_truncated` counters make
discarded diagnostics explicit; no response body, secret, raw URL or
diagnostic identifier is added to those rows. These bounds do not change the
profile rates, durations, thresholds, status counters, correctness checks or
percentile math.

CPU-profile files whose PID is not among the workers armed for this observer
window are ignored for the private authoritative summary, never deleted, and
remain caller/host retention artifacts. The private observer reports that
preservation explicitly; unbound profile files are not evidence for this run
and are not a cleanup error by themselves. Before upload, the
producer-specific public projection retains enabled/profile-count and numeric
aggregate fields, plus a bounded function summary for each available CPU
profile. It never retains worker PIDs or identities. Missing required observer
evidence remains a hard failure.

### Evidence privacy boundary

Load, observer and live-QA reports cross a fixed-schema boundary before they
are persisted or uploaded. They retain population counts, throughput,
response/status counts, route classes/templates, numeric timing distributions,
query IDs plus allowlisted query/backend/wait categories, and bounded CPU/DB/
Redis/process aggregates. They do not retain control/operator email values,
tournament slugs, raw URLs/queries, request/diagnostic/correlation IDs, user
digests, edge IP/location, Cloudflare ray values, cookies, authorization or
SQL text. Unknown route/error/backend values collapse to `other`; control
account state is only `control_account_preserved: bool`.

For production external-load runs, the fixture `matrix-summary.json` is a
pre-cleanup measurement/setup receipt and deliberately leaves control-account
preservation false. The later `cleanup-summary.json` is the authoritative
preservation receipt: it is emitted only after the run-bound cleanup verifies
the designated control account remains and the synthetic fixture is removed.
Keep those stage-specific booleans separate when reviewing artifacts.

CPU profile projection is capped at 32 profile summaries and 100 function rows
per profile. It keeps only the availability flag, a restricted Python symbol
token (unsafe names become `other`), and bounded line/call/timing numbers.
Integer fields require JSON integers from 0 through 1e12; timing fields must
be finite values from 0 through 1e12. Source filenames, profile paths, raw pstats data,
PIDs and all unrecognized profile fields are discarded before publication.

Remote command output is captured in a mode-0600 temporary file, reduced to
one bounded canonical summary, and deleted before the fixed JSON evidence is
uploaded. Required load/QA/live artifacts use `if-no-files-found: error` and a
14-day maximum retention. The raw attempt and logical-action populations,
accepted latency percentiles, throughput/goodput, status/error classes, pool
waits, DB wait-state counts, CPU/RSS, and observer completion/binding checks
remain mandatory acceptance inputs.

The tournament-lifecycle QA contour is intentionally separate from this
registry and is executed only by `platform_production_qa.py` against the
configured QA/preprod origin. It has no selectable production-load profile and
must not invoke the external 15k/20k workflows. The harness reports each
lifecycle phase with full HTTP request/success/error/percentile/throughput/
goodput/response-byte metrics plus the existing system sampler and diagnostic
`request_perf` data.

When API request-performance logging is enabled, `request_perf` retains its
existing slow, error and diagnostic triggers and also emits every sixteenth
completed HTTP request per API process. Each retained row identifies only its
selection reason, process-local completion count and interval. These rows form
a trigger-selected diagnostic sample: their pool quantiles describe logged
rows only and never replace or claim the full-population HTTP client metrics.

The v2 SLO profile applies the supported-load contract: accepted request
p50/p90/p95/p99 of 250/400/600/1000 ms, logical p95/p99 of 600/1000 ms,
final logical failure below 0.5%, and effectively zero overload shedding. A
capacity ramp reports SLO capacity and maximum stable goodput separately.
Stress and spike profiles deliberately do not inherit the final-failure SLO;
they evaluate bounded latency, retry amplification, shedding, minimum useful
goodput, origin CPU/DB pool/wait evidence, durable correctness and cleanup. A
stress result may end
with many logical failures and still conclude `STRESS BEHAVIOR PASS`; that is
not a capacity-target pass.

The capacity runner allocates unique fixture users across rate phases and
paces each phase for its declared steady duration. The spike runner preserves
phase boundaries so pressure entry, peak latency, shedding, goodput, retry
amplification and recovery can be compared instead of hidden in one aggregate.
Capacity reports explicitly distinguish `experiment_complete` from
`target_passed`; a complete run with a failed target is not green.

The measured HTTP generator runs only on the GitHub-hosted runner. The
production origin may prepare marked fixtures, collect lightweight pressure
evidence and perform exact cleanup; it must not execute the measured client.

The authenticated HTML transport diagnostic records the full hop boundary:
Next.js direct, local Nginx and public Cloudflare. Source-level Nginx policy
disables response buffering only in the dynamic HTML proxy location and emits
encoding/transfer/buffering headers into the existing JSON access log. The
`web-ssr-diagnostics` profile also reports interval event-loop delay p50/p95/p99,
ELU, process CPU and GC time. These diagnostics are not a production result
until the unchanged control and the exact external profile are rerun in the
same window. The bounded operator probe is
`tools/platform_ttfb_probe.py`; it does not replace the external load gate.

The authenticated page-load client is versioned separately from the production
runtime. `authenticated-page-load-v1` remains the canonical historical control
and uses the existing urllib HTTP/1.1 connection-close behavior. The explicit
`authenticated-page-load-v2` candidate uses one bounded HTTP/1.1 keep-alive
connection per external-runner worker and records DNS, TCP, TLS, request-write,
header-wait, TTFB, body and total timings, plus HTTP version and reuse counts.
It is a controlled client-transport A/B, not a browser or HTTP/2 simulation:
compare v1 and v2 only on the same reviewed source, runtime profile and clean
fixture window. The v2 transport does not change application authentication,
cookies, validation, retry policy, origin routing or production defaults.

When `web-ssr-diagnostics` is explicitly active, each interval event-loop line
also exposes the Node PID, process RSS, V8 heap total/used, external memory and
ArrayBuffer bytes. The retained observer summary places aggregate values under
`event_loop.memory` and per-worker values under `event_loop.by_pid`; missing
fields remain an absence of opt-in telemetry, not zero memory.

Current resource-safety semantics are explicit: `max_postgres_backend_connections`
is evaluated from the observer's `pg_stat_activity` backend count and grouped
`postgres_backend_ownership`; `postgres_tcp_established_connections` is a
separate `/proc/net/tcp*` socket diagnostic. Historical reports retain their
original TCP-based wording and decisions.

## External authenticated-page and CPU diagnostic checkpoint (2026-10-18)

The deployed application source remained `3c3b060bfb8927aa3a9ee5896a40ed1a0f6c8c84` through both authenticated-page measurements and the CPU diagnostic attempt; the CPU pair itself did not start. Profile 10 (`authenticated-page-load-v1`, HTTP/1.1 close) completed 20,000/20,000 HTTP actions with no errors, retries or drops. HTTP p95/p99 were 3,095/3,935 ms; full user-observed logical p95/p99 were 587/618 seconds. Its report bindings and fixture freshness passed review, and authoritative cleanup reported `ok=true` and `control_account_preserved=true` after deleting the 20,000 test users and 40 tournaments. The result is a valid SLO failure.

Profile 11 (`authenticated-page-load-v2`, HTTP/1.1 keep-alive) also completed 20,000/20,000 actions with no errors, retries, drops or duplicates. HTTP p95/p99 were 2,742/3,159 ms; logical p95/p99 were 522/542 seconds. Bindings, new-fixture identity and authoritative cleanup passed independent review; cleanup again preserved the control account. This is also a valid SLO failure. Keep HTTP request timing distinct from full logical user-observed timing; the latter includes bounded scheduling wait.

The separate CPU diagnostic run `39099339471/1` is invalid/incomplete and earns no matrix credit or CPU-threshold result. Its client recorded 18,164 HTTP 200, 1,835 HTTP 502 and one HTTP 500; the bounded observer evidence confirms one `deadlock-web.service` cgroup OOM kill at `2026-10-18T05:27:31Z`, followed by one web restart. It does not establish the individual 502 causes. The CPU pair did not start: the pair runner rejected the missing `SOURCE_GIT_SHA` required to bind the runner source independently from `APP_TARGET_SHA`. Source review also found that the pair's CSRF validator rejected the issuer's canonical `43 URL-safe characters + '.' + 64 lowercase hex` token shape. A reviewed local tooling fix addresses those two defects without changing application behavior, authentication rules, or diagnostic thresholds. The run is not a threshold failure and does not support a CPU capacity conclusion.

Before another CPU measurement, provide a separately guarded CPU-only path that preserves the eight-actor fixture, two Gunicorn workers plus one Node process, OFF/ON request counts, all pairing and no-restart checks, and existing thresholds. Do not rerun the current profile route blindly: it runs a 20,000-action load before the pair, and the observed web cgroup OOM makes that repeated stress inappropriate without a reviewed plan. Preserve the failed diagnostic evidence and keep production memory limits and security checks unchanged.

## Performance and backup checkpoint (2026-10-14)

The five-run matrix remains **PASS / PASS / PASS / FAIL / FAIL** for
`ready-vote-slo-v2` (38161685678), `read-mix-human-v2` (38162117136),
`ready-vote-spike-v1` (38162697642), `ready-vote-capacity-ramp-v2`
(38163212410) and `ready-vote-stress-15k-v2` (38165101863). Preserve each
result against its original source and contract. The capacity profile's
highest qualifying SLO rate is 60 logical actions/s. At 70/s, accepted and
logical p95/p99 were 621.066/7,326.342 ms and 621.453/7,326.683 ms, over the
600/1,000 ms limits. At 80/s, latency passed but retry amplification (0.3333%)
and shedding (0.3322%) exceeded the profile's zero limits. Stable goodput was
79.355 actions/s; it is a separate measure, not the SLO capacity or a proven
server limit.

The 15k stress profile failed: logical p95 was 109,811 ms versus HTTP p95
439.348 ms; final logical failures were 22.273%, shedding 60.5453%, and retry
amplification 97.0032%. These client outcomes do not establish CPU saturation
or attribute a server-side cause. The report-only retry-queue correction in
candidate source 5df binds retry HTTP attempts to their actual same-worker
execution; the historical retry queue interval included prior HTTP/backoff
time and is not server queue evidence. The correction does not change offered
load, concurrency, acceptance thresholds or security behavior, and the
candidate is not credited as deployed.

Exact-SHA Security/build run
[`38630875202`](https://github.com/StrayForest/old_sparky/actions/runs/38630875202)
for source `5dfb95c211ed37d93fde31201cb768560b1fd6c8` completed with 16
successful jobs and 2 expected skips. No eligible backup reservation preceded
its all-job maximum, so this run cannot prove a fresh backup. The saved native
observation for automatic chain
[`38632646564`](https://github.com/StrayForest/old_sparky/actions/runs/38632646564)
was still at the exact-source backup-verification step, before production
dispatch; this checkpoint contains no terminal chain result, restore/Alembic
pass or deployment proof. Earlier failed backup observations remain distinct:
one measured a 13,541,376-byte reserve shortfall at the captured controller
poll; a separate partial resource capture was 17,543,168 bytes below the
1 GiB reserve. Do not infer OOM, a precise later phase, or a common cause from
these observations.

Separate bounded local resource reads on 2026-10-14 observed `MemAvailable`
of 1,013,882,880 bytes, then 1,069,637,632 bytes in a later lightweight read;
both were below the 1 GiB reserve and are not an admission pass. A worker
cgroup read observed `memory.current=291,667,968` and `memory.max=1,073,741,824`
with zero recorded OOM counters. The read was partial because
`cpuset.cpus.effective` was absent; effective host CPU capacity and host
attribution remain unknown. A bounded task-path process scan was also
partial, with 157/157 numeric PID entries visited, zero exact selector matches,
and 103 `/proc/<pid>/exe` lookup errors reported as ENOENT; affected processes
and the cause were not identified. It does not establish historical process
completion. No tmpfs object has proven cleanup ownership; protected and unknown
roots remain retained.

External browser/Turnstile human QA is **NOT RUN**. Restore/Alembic, automatic
production deployment, the remaining six profiles, final CPU/automated QA,
retention/maintenance and external gate closure are not established by this
checkpoint. A later bounded source-prefix read found no currently loaded
matching scratch units; it did not establish absence of mountpoints/files or
cleanup ownership. Protected and unknown roots remain untouched pending new
source-bound evidence.

## Backup and restore follow-up (2026-10-15)

Two later source-bound backup attempts failed the unchanged 1 GiB memory
reserve guard: run `38662792582` reported a 786,432-byte deficit and run
`38666349694` reported a 5,001,216-byte deficit. The latter used the paired
scratch PostgreSQL `shared_buffers=32MB` initialization/start candidate, which
the [backup and restore runbook](../docs/backup-restore-runbook.md) records as
reverted to the prior paired 64 MiB setting. It did
not complete restore/Alembic verification, so there is no causal comparison,
measured memory-saving result or demonstrated effect on the reserve refusal. Keep
durability, authentication, concurrency, confinement, tmpfs, MemoryMax,
host-reserve and timeout settings unchanged. The evidence does not establish
OOM or a common cause for the failures.

Security run `38671815760`, attempt 2, passed all 18 jobs for source
`16d956c63c1b0360d147b7916c9e513fe9b4876c` (16 success, 2 expected skips).
The one bound backup create, run `38722295410` attempt 1, failed the unchanged
1 GiB reserve in `restore_platform`: the observed `MemAvailable` was
1,066,323,968 bytes, 7,417,856 bytes below threshold. Scratch cleanup is
confirmed, while restore/Alembic completion is UNKNOWN. No pair was produced,
so the automatic deployment chain did not run; the prior 20 pairs and four
protected roots remain preserved. One cgroup observation recorded
`memory.current=494,878,720`, `anon=36,368,384`, `file=452,292,608`, and
`shmem=416,301,056` bytes, with zero reported high/max/oom/oom_kill events at
that sample. This is not a peak or causal attribution. That attempt's report
did not retain aggregate PGDATA/base/pg_wal allocation; the later run below
did. `tree.data` includes nested `base` and `pg_wal`; these are overlapping
views and must not be added. Attribution `complete` means the expected samples
were collected, not that restore, Alembic, or backup verification succeeded.

The bound create `38738894181`, attempt 1, used source
`a9360edb0c914f3240654ac4d9755df968db115b` and failed the same 1 GiB
`parent_active_poll` reserve guard. `MemAvailable` was 1,063,047,168 bytes,
10,694,656 bytes below threshold. Scratch cleanup was confirmed; restore and
Alembic completion remain UNKNOWN, no backup pair was produced, and the natural
auto-deploy parent failed its source-bound backup guard without dispatching a
child. The original 20 backup pairs and four protected roots remain preserved.
The private attribution was partial because no restore-end sample was
available. Its phase points were:

| Sample | Offset / interval | MemAvailable | 512 MiB tmpfs used | `tree.data` logical / allocated (files / dirs) | `base` allocated | `pg_wal` allocated (files) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `initdb_end` | 1.698 s | 1,392,975,872 B | 40,075,264 B | 40,029,512 / 40,075,264 B (968 / 26) | 22,585,344 B | 16,777,216 B (1) |
| `postgres_start_end` | +0.185 s | 1,386,594,304 B | 41,082,880 B | 41,018,434 / 41,074,688 B (971 / 26) | 22,585,344 B | 16,777,216 B (1) |
| `restore_platform_start` | +0.384 s | 1,381,892,096 B | 43,540,480 B | 48,974,437 / 43,532,288 B (1,272 / 27) | 24,985,600 B | 16,777,216 B (1) |
| `parent_last_guard` | +9.760 s (6 ms guard lag) | 1,063,047,168 B | 309,264,384 B | 309,979,749 / 310,050,816 B (1,368 / 27) | 157,286,400 B | 150,994,944 B (9) |

From restore start to the last guard sample, tmpfs usage rose by 265,723,904
bytes, `base` allocation by 132,300,800 bytes, and `pg_wal` by 134,217,728
bytes. These are interval observations, not an atomic peak or proof that WAL
alone caused the reserve refusal; `tree.data` already contains both nested
subtrees. The source selects PostgreSQL 16 but does not record the patch
version used by this attempt. It also passes no WAL or checkpoint overrides.
PostgreSQL 16 documents default `max_wal_size=1GB` as a soft automatic
checkpoint threshold, `min_wal_size=80MB` as a checkpoint recycling floor,
and a five-minute default checkpoint timeout
([WAL configuration](https://www.postgresql.org/docs/16/runtime-config-wal.html)).
Those defaults are large relative to this 512 MiB scratch tmpfs and the roughly
10-second restore interval, while nine 16 MiB WAL segments were present at the
guard sample. This supports a scratch-only checkpoint/recycling experiment,
not a guaranteed WAL cap or memory saving.

The reviewed local source candidate adds `max_wal_size=64MB` and
`min_wal_size=32MB` only to the disposable scratch PostgreSQL startup. It
leaves `wal_level`, `fsync`, `full_page_writes`, `synchronous_commit`, all
resource guards and timeouts unchanged. These settings may cause more
checkpoint I/O/CPU; `max_wal_size` is not a hard cap, and a lower WAL recycling
floor is useful only after checkpoints complete. The candidate is not deployed
and has no restore/Alembic result yet. Do not treat telemetry completion or a
smaller observed `pg_wal` as backup success, capacity admission, or an
application throughput result.

The published WAL-only candidate's bound create `38746516837/1` then failed the
same reserve guard by 2,363,392 bytes. Its last partial scratch sample recorded
240,975,872 allocated bytes in `base`, 67,108,864 in `pg_wal`, and 309,854,208
in `tree.data`; `tree.data` already includes both child paths. Cleanup was
confirmed, while restore/Alembic completion and the full scratch peak remain
UNKNOWN.

The next bound create, `38759048702/1` on source
`6f46b43da63f19ce0ae8ede29712d79e70d3c46e`, failed in `public_summary` with
exit 1. The helper returned success and the closed report validator accepted
the restore/Alembic success fields, but the external workflow failed; the
normalizer cause remains UNKNOWN because its stderr was redirected and the
report body was not retained. The natural auto-deploy guard failed without
dispatching a child. A metadata-only inventory found 21 dump/sidecar pairs and
stat-matched the 40 original entries to their saved nine-field baseline. The
new sidecar declares restore/Alembic verification, but its archive SHA was not
recomputed and it is not cryptographically bound to this run. This is not an
external backup success. A private diagnostic records normalizer exit,
bounded stdout/stderr lengths and hashes, fixed classes, and allowlisted
exception class; it does not change acceptance. Its stdout readback cap is
1,056,768 bytes and its stderr readback cap is 65,536 bytes; both are checked
after the normalizer writes temporary files, so they bound diagnostic reads and
hashing, not temporary-file growth.

The later create `38781187992/1` on source
`3f42b5686dc683418653c7931a4d8952f29bc6ce` also failed at `public_summary`
after the closed report validator accepted the helper report; the success
receipt was skipped. Its private normalizer diagnostic recorded zero stdout
bytes and 58 stderr bytes with SHA-256
`1b6eab82de5b2e21bfe75bbde1b6fbc8354135fbd6c8349b676eec57c0e94970`.
Offline execution of the exact remote caller reproduced those bytes: the
remote Bash script runs with `nounset`, receives the two cache flags as
positional arguments and assigns lowercase variables, while the normalizer
call expanded unassigned uppercase names. Bash exited before starting Python,
leaving stdout empty. The local correction passes the already validated
lowercase flags and does not alter summary validation, backup acceptance,
retention or restore behavior. The reviewed local fix remains unpublished and
undeployed; the regression is in
`PlatformStorageMaintenanceTests.test_manual_backup_uses_lock_aware_backup_only_workflow`
under `backend-privileged`, and no new bound CREATE or retry was dispatched.
This explains only run
`38781187992/1`. The cause of the preceding `38759048702/1` remains UNKNOWN,
and the original confinement/restore failure `38261983678` also remains
UNKNOWN; do not relabel either as this callsite bug.

The external 11-profile matrix remains 5/11 complete; this workflow repair
adds no load-profile result or server-capacity evidence. Human browser and
Turnstile QA, the remaining six profiles, separate CPU validation, final QA,
and healthy maintenance-timer completion remain outstanding.

A read-only snapshot of the current live `platformdb` (2026-10-15 14:52:18
UTC, not bound to the failed archive) counted 36 platform physical relations:
`pg_table_size` 272,400,384 bytes, attached indexes 3,571,712 bytes and combined
relation total 275,972,096 bytes. Whole-database size was 286,825,495 bytes.
The visibility flags for GUCs and activity statistics were false, so those
returned fields are visibility-limited; this is a current database inventory,
not the failed archive's complete scratch footprint or a restore peak.

The disposable PG16.15 startup measurement found that `shared_buffers=16MB`
reduced fixed main shared-memory allocation by 53,411,840 bytes versus 64 MiB,
including derived `wal_buffers`. A rough scratch estimate is current platform
relation bytes plus the 40,075,264-byte empty `PGDATA` baseline, less its initial
16 MiB WAL, plus the observed 64 MiB WAL point: 366,379,008 bytes (349.41 MiB).
This is not an upper bound: the live snapshot is not archive-bound, the failed
scratch sample was partial, and `max_wal_size=64MB` is a soft checkpoint
threshold rather than a cap. The next local candidate sets `shared_buffers=16MB`
consistently in `initdb` and server startup for the disposable restore cluster
only. A smaller cache may increase restore I/O or CPU; full restore/Alembic
success remains unproven. WAL policy, durability settings, resource guards,
confinement and timeouts stay unchanged.

The source-pinned provider-only repair restored the attested legacy
`19603dfff122386726313c86b0350812ebbe8cca` LiveQA runtime provider referenced
by protected consumers. The installed 9ce provider verifier passed under the
canonical release lock; current/previous application pointers were preserved.
This was not an application deployment or end-to-end QA pass.

After exact source and reference checks, two duplicate tmpfs artifacts were
removed individually while byte-identical private disk copies and manifests
were retained through final QA: release ZIP run `11660433667` (72,206,034
bytes, SHA-256 `7d1b4ed44559c144d58835baa2a3808f8be15c1f8f88c8f70c53bfa4e03b52e0`)
and source tar run `11674976728` (72,302,841 bytes, SHA-256
`785f31186d922f86176eaba22b7be2e1ed89fcf517eab943943dc3da15f4427f`). The
source73 reference report remains PARTIAL because its outer collector return
was exit 1 with truncated stdout; the independently preserved inner report
and offline comparison support only the exact provenance/checksum marker
pair. A separate COMPLETE FD/maps/map_files scan covered the exact tar inode
at one current point, not future opens or an atomic lease. All 20 backup pairs,
four protected `/run` roots, unrelated siblings, historical 65a, and retained
copy/manifest evidence remain preserved.

A post-cleanup point read recorded MemAvailable `1,302,296 kB`, `/dev/shm`
available `1,158,631,424 bytes`, root filesystem available `7,858,356,224
bytes`, and memory PSI some/full avg10 `0.00`. These observations are not an
admission pass or root-cause finding. A fresh locked provider verification
passed, but restore/Alembic, normal automatic deployment, the remaining six
profiles, separate CPU validation, browser/Turnstile QA and timer completion
remain NOT RUN / UNKNOWN. Preserve the existing 1 GiB reserve and all profile
thresholds.

## Historical production performance stage (2026-09-07)

The 2026-09-07 performance stage is closed historical evidence with an
explicit authenticated HTML follow-up. The exact 13-profile repeat used the
then-current contracts and thresholds on deployed SHA
`bba3fb278e348906a6942aee8462b758c3d616ef`; lifecycle profiles remain
preproduction-only. The full evidence table, run links, change ledger,
origin-safety results and cleanup proof are in the archived
[2026-09-07 performance-stage report](../docs/archive/performance-stage-2026-09-07.md).
Do not treat this matrix as the current portfolio or as evidence that the
current timing, observer-binding or Redis projection cleanup contract was
executed.

The historical run reported all 13 profiles passing their then-declared
acceptance and exact cleanup. Backend
peaks were 51–52 against the 52-backend budget. Normal Ready Vote/read traffic
had no unexpected statuses, timeouts, 520s or 522s. Stress profiles returned
only their declared controlled `503` overload responses, while read profiles
completed their 200/304 contracts without errors or retries.
That historical cleanup result is preserved as reported; it is not
retroactively upgraded to proof that the current per-key Redis projection
cleanup deleted `teams`, `workspace_detail`, `bracket_summary` and
`bracket_full` with zero residual keys.

The authenticated page returned 20,000/20,000 HTTP 200 responses, with total
p95 `3357.939 ms` and HTML TTFB p95 `2568.859 ms`. The D9 shell projection is
functionally clean, but this run is not a same-window unchanged-code A/B and
the requested authenticated TTFB target `<1,000 ms` remains open. The final
attribution for the bounded authenticated HTML/TTFB investigation is archived
in
[`performance-authenticated-html-ttfb-2026-09-07.md`](../docs/archive/performance-authenticated-html-ttfb-2026-09-07.md);
the browser-workspace candidate is archived in
[`performance-authenticated-html-ttfb-workspace-client-2026-09-08.md`](../docs/archive/performance-authenticated-html-ttfb-workspace-client-2026-09-08.md),
the avatar-defer candidate is archived in
[`performance-authenticated-html-ttfb-avatar-defer-2026-09-08.md`](../docs/archive/performance-authenticated-html-ttfb-avatar-defer-2026-09-08.md),
and the rejected chrome-boundary candidate is archived in
[`performance-authenticated-html-ttfb-chrome-boundaries-2026-09-08.md`](../docs/archive/performance-authenticated-html-ttfb-chrome-boundaries-2026-09-08.md).
The rejected admission candidate and its exact A/B result are archived in
[`performance-authenticated-html-ttfb-admission-2026-09-07.md`](../docs/archive/performance-authenticated-html-ttfb-admission-2026-09-07.md).
The first merged overlap candidate was deployed in release
`4db0079fcddba6fa37bd089d2a96599c04d02768` and measured by exact external run
`34145804669`: all `20,000/20,000` HTML responses were HTTP 200 with exact
cleanup, but TTFB p95 was `2527.624 ms`, only `1.605%` below baseline. The
follow-up SSR-only diagnostic run `34148261104` measured post-data
unattributed upstream p95 `2340.311 ms` after detail data-ready p95
`1112.536 ms`; production was restored to ordinary `ready-vote-static-8`.
The archived route-local Suspense candidate improved TTFB p95 to
`2325.559 ms` in exact run `34153656342` but did not close the target. The
rejected client detail-boundary candidate was measured by exact external run
`34159422212`: it returned `20,000/20,000` HTTP 200 responses with exact
cleanup and TTFB p95 `2270.945 ms`, a safe incremental result that did not
close the target. The archived candidate deferred the tournament workspace
read itself to the browser while retaining the server-rendered authoritative
auth/header path. Its exact external A/B `34169435362` measured HTML TTFB p95
`1,595.286 ms` with `20,000/20,000` HTTP 200 responses and exact cleanup. The
diagnostic repeat `34171146327` measured upstream-header p95 `1,141 ms`,
upstream-connect p95 `1 ms`, sampled root auth-bootstrap p95 `95.222 ms` and
sampled root component-tree p95 `96.373 ms`; production was restored to
`ready-vote-static-8` by `34172262955`. The avatar candidate removed only the
optional avatar SQL from the blocking bootstrap request. The chrome-boundary
candidate was rejected after exact runs `34181055402` and `34182385484` both
failed to complete the 20k/40 client load; both fixtures were cleaned by
guarded abort/cleanup workflows. A post-rollback diagnostic deployment
`34185100223` and load `34185320306` repeated the pre-report stall; guarded
abort `34186422087` and exact cleanup `34186465537` passed with 20,000 users,
40 tournaments and zero remnants. Production was restored to
`ready-vote-static-8` by deploy `34184805970` for reviewed SHA
`3d7aca0e832bac3c79e1b391e7817b74e5695b03`. The root component-tree
diagnostic is archived in
[the measured root component-tree report](../docs/archive/performance-authenticated-html-ttfb-root-component-tree-2026-09-08.md).
The root render/flush boundary candidate
[`active-authenticated-html-ttfb-root-render-flush-2026-09-09.md`](active-authenticated-html-ttfb-root-render-flush-2026-09-09.md)
is frozen pending transport evidence; no capacity-setting change is
authorized. The reviewed transport-observability package is deployed at
source SHA `1d25ae335f3eb749ce36b4fe97f868d5f357803c` through production deploy
[`34325036735`](https://github.com/StrayForest/old_sparky/actions/runs/34325036735).
Its Nginx policy, Node event-loop/CPU/GC diagnostics and bounded hop probe are
diagnostic instrumentation only until the unchanged authenticated external
control is rerun. The active next step is the
[`authenticated HTML transport runbook`](../docs/performance-transport-runbook.md).
The source also exposes the isolated operator profile
`web-ssr-native-transport`, which changes only the trusted loopback auth GET
transport while retaining the one-worker baseline contour. The existing
`web-ssr-workers-2` profile remains a combined worker-plus-transport candidate;
neither profile is the production default without a clean, same-contour A/B.
The Next.js 16.3.4 security baseline
and its new unchanged control are archived in
[`security-web-dependencies-next-2026-09-09.md`](../docs/archive/security-web-dependencies-next-2026-09-09.md).
The control is run `34287694375` with TTFB p95 `1725.436 ms` and full-page
p95 `2279.541 ms`; it replaces the historical 16.2.12 result as the only
valid control for a future A/B. The narrower authenticated-provider-boundary candidate is
archived in
[`performance-authenticated-html-ttfb-provider-boundary-2026-09-08.md`](../docs/archive/performance-authenticated-html-ttfb-provider-boundary-2026-09-08.md).
It moved only `SiteFooter` outside `AuthProvider`, preserving the authoritative
server-rendered authenticated header and all API, SQL, worker, pool, admission,
permission and security behavior. Reviewed SHA
`6e3a6325a0c6bee025297a8c8edb00817faafb75` completed the exact 20k/40 profile
in [run 34192721178](https://github.com/StrayForest/old_sparky/actions/runs/34192721178):
20,000/20,000 HTTP 200, zero errors/unexpected/overload/retry responses,
HTML TTFB p95 `1341.322 ms`, total page p95 `1665.096 ms`, PostgreSQL peak
`43/52`, zero lock waiters, two workers and exact cleanup. This is a `47.8%`
TTFB p95 improvement from the unchanged `2568.859 ms` control but still misses
the `<1,000 ms` target by `341.322 ms`, so the candidate was rejected and
reverted through the reviewed `dev` path. The follow-up global-chrome
boundary candidate is archived in
[`performance-authenticated-html-ttfb-global-chrome-2026-09-08.md`](../docs/archive/performance-authenticated-html-ttfb-global-chrome-2026-09-08.md).
Its reviewed source SHA `0cb0f1fabafa793d0520774a04883b02ad4a2584` completed
the exact 20k/40 profile in [run 34216385147](https://github.com/StrayForest/old_sparky/actions/runs/34216385147):
20,000/20,000 HTTP 200, zero errors/unexpected/timeouts/overload/retries,
HTML TTFB p95 `1391.415 ms`, total page p95 `1704.678 ms`, PostgreSQL peak
`44/52`, zero lock waiters, two workers and exact cleanup. Origin safety passed,
but the candidate missed the target by `391.415 ms` and was worse than the
narrower provider-boundary candidate, so it was rejected and reverted through
the reviewed `dev` path. Production was restored to `ready-vote-static-8` by
automatic deploy `34234377917` for rollback SHA
`fe48877e3c7e10549bdd4a50afad5d5a9744b2f6`. The tournament-detail-footer
candidate is archived in
[`performance-authenticated-html-ttfb-detail-footer-2026-09-08.md`](../docs/archive/performance-authenticated-html-ttfb-detail-footer-2026-09-08.md).
Its exact load `34230327945` reached the full 20k/40 fixture but failed closed
on a client `IncompleteRead` before producing a TTFB report; exact cleanup
passed with zero remnants. The active diagnostic work order above remains the
only next performance step; no capacity-setting change is authorized.
The execution record remains available in
[the archived 2026-09-07 work order](../docs/archive/performance-stage-request-2026-09-07.md), now closed
with that explicit follow-up.

## Read-path candidate slice (2026-09-03)

The retained read-mix baseline remains the comparison point: source
`4be82f1a…`, workflow `33625164162`, raw p50/p95/p99 `1212/2382/3492 ms`,
99.42 requests/s, API CPU about 98% per core and zero errors. The following
read-path changes were then accepted on exact-SHA production evidence:

- request-performance now distinguishes checkout wait, connection hold,
  SQL time and total request time;
- workspace and `/users/me` use an early session release after model
  materialization, while the workspace hot preflight uses primitive column
  snapshots instead of ORM hydration;
- global SSR auth uses `/auth/bootstrap`; full `/users/me` remains for profile
  and settings/account-security surfaces;
- the external runner has `read-mix-concurrency-ramp-v1`, which records every
  c16–c128 full-population stage separately;
  `capacity_ramp` is auxiliary evidence rather than a user-visible phase, but
  its authored stage keys, order, populations and per-stage budgets are
  mandatory; missing, altered or unknown auxiliary stages fail closed;
- `uvloop`/`httptools`, Nginx upstream keepalive, pool sizes 12/16/20/24,
  `pool_pre_ping=false` and authenticated-read admission 32 were isolated
  runtime A/B profiles.

The accepted runtime keeps JSON encoding, media loading, the
security-sensitive Redis session-validity model, and the two-worker/API-pool
envelope unchanged. Production uses `authenticated-read-admission-32`, API
pool `24` with `max_overflow=0`, `pool_pre_ping=true`, and the PostgreSQL
safety budget remains `52`. The final profile was applied by exact-SHA deploy
[`33726577559`](https://github.com/StrayForest/old_sparky/actions/runs/33726577559).

### Profile cache and upstream keepalive candidate (measured 2026-09-03)

The current development candidate changes profile-read access to a GET-first
lookup on the process-local shared Redis pool, taking the single-flight lock
only after a miss. A profile-less user is represented by a versioned 60-second
negative sentinel, so repeated `/auth/bootstrap` requests do not repeat the
profile builder SQL; profile creation and mutation paths invalidate the entry
after commit. The Nginx HTML route also uses a named `platform_web` upstream
with HTTP/1.1 keepalive and the inherited cleared `Connection` header.

The candidate was deployed by production run `33755347161` from source
`19e1f246cb97fed13671bfb9e234e18904522a2b` and measured by the exact-cleanup
external run [`33760930891`](https://github.com/StrayForest/old_sparky/actions/runs/33760930891)
using `authenticated-page-load-v1` (version 1, digest
`fe29f8bf227c78e301d86d4775792bc21f02ac8f17fcc39d1a92dcfece966b57`) at c64:

| Population | Requests | p50 ms | p95 ms | p99 ms | Max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full HTML page | 20,000 | 2295 | 3795 | 4417 | 8092 |
| HTML TTFB | 20,000 | 1744 | 3250 | 3852 | 7613 |

All 20,000 responses were `200`, with zero errors, zero retries and zero
shedding; wall time was `731.458 s` at `27.343` requests/s. The origin observer
recorded `/bootstrap` at 15,480 requests with average SQL count `2.0`, server
request p95/p99 `2051/2415 ms`, and the normalized profile-builder statement
at 15,480 calls. This workload gives each fixture user one page load, so it
does not itself exercise repeated negative-cache hits. Origin PostgreSQL
connections peaked at `51` (`48 api + 2 worker + 1 observer`), lock waiters
peaked at `0`, and API process CPU averaged `87.22%`.

The same run also verified Nginx upstream reuse: all 20,000 marked HTML
requests went to `127.0.0.1:3000`; `connection_requests` had median `266` and
maximum `1000`. This confirms that the named `platform_web` keepalive is active
on the real HTML path.

An isolated loopback probe against the deployed API used a temporary active
session whose user had no `PlayerProfile`, then removed the user/session and
both Redis keys. Across 20 sequential `/auth/bootstrap` requests, the
negative-sentinel phase produced one profile SELECT (the first fill) and 20
auth SELECTs; forcing a profile miss before every request produced 20 profile
and 20 auth SELECTs. A positive-cache phase produced 20 Redis `GET`s with no
`SET` or `EVAL`. Therefore hypothesis 1 is confirmed for warm reads and
hypothesis 2 is confirmed for repeated absent-profile requests: the profile
SQL path changes from `N` to `1` over the sentinel TTL, while authoritative
session SQL remains per request.

Compared with the retained prior page A/B (`33744391709`), the candidate moved
full-page p95/p99 from `3901/4426 ms` to `3795/4417 ms` and HTML TTFB p95/p99
from `3423/3796 ms` to `3250/3852 ms`. This is directional evidence rather
than a clean same-window A/B; the causal cache conclusions above come from the
isolated `pg_stat_statements` and Redis-command probe. Hypotheses 3, 4, 6 and 7
remain unmeasured.

### Read-path ownership, ramp and SSR evidence (2026-09-03)

The exact-SHA concurrency ramp
[`33716010545`](https://github.com/StrayForest/old_sparky/actions/runs/33716010545)
used the full 20,000-user read mix. Its stage results were:

| HTTP concurrency | p95 ms | p99 ms | Throughput req/s |
| ---: | ---: | ---: | ---: |
| 16 | 288.658 | 374.756 | 80.552 |
| 32 | 529.440 | 685.602 | 104.298 |
| 48 | 1080.505 | 1333.289 | 95.630 |
| 64 | 1040.676 | 1264.266 | 101.799 |
| 80 | 1436.423 | 1722.248 | 99.403 |
| 96 | 1354.994 | 1687.727 | 103.961 |
| 112 | 1855.392 | 2253.597 | 95.488 |
| 128 | 1589.240 | 1925.639 | 108.532 |

The report identifies `32` as the stable knee and `48` as the first queued
stage: p95 rose materially while throughput did not. Admission limit 32 was
therefore selected by the operator and validated in the authenticated-page
A/B; the ramp never changes runtime limits automatically.

The real Next.js HTML benchmark
[`33719680133`](https://github.com/StrayForest/old_sparky/actions/runs/33719680133)
at c64 had no errors or bad statuses, but exposed uncontrolled origin queueing:
full-page p95/p99 `10378/10714 ms`, HTML TTFB p95 `3062 ms`, and pool checkout
p95 `10001 ms`. With `authenticated-read-admission-32`,
[`33722487079`](https://github.com/StrayForest/old_sparky/actions/runs/33722487079)
passed the stress-behavior gate with no errors or shedding: full-page p95/p99
`3906/4425 ms`, HTML TTFB p95 `3440 ms`, pool checkout p95 `533 ms`, and
goodput `25.497` versus `17.83` requests/s. This is a diagnostic stress
comparison, not a claim that the public production SLO is the same as the
20,000-request benchmark.

The PostgreSQL observer attributed the peak of `51` established connections
to API `48`, worker `2` and observer `1`; one transient sample was unknown.
This is within the `52` safety budget, so the API pool was not reduced or
expanded. The observer also records normalized `pg_stat_statements` rows and
excludes only its own diagnostic queries. Exact cleanup after the ramp
([`33719571565`](https://github.com/StrayForest/old_sparky/actions/runs/33719571565)),
page baseline, page A/B and pre-ping A/B removed all fixture users,
tournaments, sessions and audit rows; the designated control account was
preserved.

The isolated `pool_pre_ping=false` stress A/B
[`33725036233`](https://github.com/StrayForest/old_sparky/actions/runs/33725036233)
passed functional stress checks but was not selected: p95/p99 were
`1741/2177 ms` at `105.367` requests/s, worse than the retained accepted
comparison, while stale-connection protection was removed. Production was
restored to `pool_pre_ping=true` in the final deploy above.

Public/static HTML remains deliberately deferred. The nonce CSP, dynamic
authenticated layout and `private, no-store` HTML policy were not bypassed or
weakened.

## Ready Vote evidence (2026-08-30/31)

The production reference was restored from baseline `e70d1e7869e36aa401f6dc9c7fd5b38fea20a597` to `ready-vote-static-8` (deploy run `33332517609`). The final measured runtime source was `e0d27295dc7990250dd0a37f0b2210ee15e5b111`; the later documentation-only release keeps the same runtime behavior. Static-8 is exact per worker: minimum/initial/maximum admission concurrency `8/8/8`, with two API workers. API pool size `24`, `max_overflow=0`, checkout timeout `10s`, Redis and the database/worker budgets were unchanged.

Canonical profile fingerprints used for the retained evidence are listed
below. The retired saturation v1–v3 rows refer to the historical files under
[`profiles/retained-v1/`](profiles/retained-v1/README.md), not the active
registry:

These are historical report digests from before the portfolio metadata block
was added; current registry digests intentionally differ and must be recorded
in new reports rather than rewriting retained evidence.

| Profile | Version | SHA-256 |
| --- | ---: | --- |
| `ready-vote-slo-v2` | 2 | `c13851df4526bb4e32ddd49b93cf2810cca2da42b19c569a2c2bc7843757543a` |
| `ready-vote-capacity-ramp-v2` | 2 | `f4956f9f0e282c44ce3adc72eeeb342cce650979336f737e032df47567ea533c` |
| [`ready-vote-saturation-ramp-v1`](profiles/retained-v1/ready-vote-saturation-ramp-v1.json) | 1 | `804c6c5f882fc41ceef6087706e3cd61db7b9c4c1c629773ad2fa32744a6451f` |
| [`ready-vote-saturation-ramp-v2`](profiles/retained-v1/ready-vote-saturation-ramp-v2.json) | 2 | `47452144eb575bd6bee2184710b8d325e43499b921875b94400a7160877a0d54` |
| [`ready-vote-saturation-ramp-v3`](profiles/retained-v1/ready-vote-saturation-ramp-v3.json) | 3 | `d34c2537469daa6be0fdefa065306a7b82db911aaf151ffaba8480fb08d65fd8` |
| `ready-vote-saturation-ramp-v4` | 4 | `be8a2da8bab1ab966acfc90863c646d011eae36fd19034bbf2df1c91e8622e17` |
| `ready-vote-stress-15k-v2` | 2 | `a9fb7897fd228a8314ee0e02bef5c11e9149045adaecddd13ee3cc4f022cc8c8` |
| `ready-vote-spike-v1` | 1 | `6351a06a342b6170bb9f7bb2a280bd4bbbdf34443b90dc5df39698a0a52c6895` |

The supported-load SLO run (`33335115575`, source `77fd8682`) issued 500
logical actions with no retries, shedding or final failures. Accepted latency
was p50/p90/p95/p99 `241.711/256.963/264.706/639.338 ms`; logical
p95/p99 was `264.831/639.447 ms`, and the maximum logical latency was
`720.010 ms`. CPU peaked at `50.46%/41.28%` on the two cores, PostgreSQL
connections peaked at `23`, and waiters/lock waiters were `0/0`. The SLO
passed. The first SLO run (`33334438643`) was rejected only because the
optional slow-request server sample was absent; client-population metrics and
cleanup were already valid under the historical contract. New exact-bound
profiles fail closed when a required server pool sample or observer binding is
absent; historical reports remain interpretable but are not retroactively
reclassified.

The controlled static-8 capacity ramp (`33335224474`, source `77fd8682`) used
30-second steady phases at 20, 30, 40, 50, 60, 70 and 80 logical actions/s.
The table reports actual offered rate, goodput, accepted p50/p90/p95/p99,
logical p95/p99, shedding, retry amplification and the phase decision:

| Target | Offered | Goodput | Accepted p50/p90/p95/p99 ms | Logical p95/p99 ms | Shed / retry | Decision |
| ---: | ---: | ---: | --- | --- | ---: | --- |
| 20 | 20.033 | 19.872 | 231.744/256.536/264.886/647.462 | 264.951/647.572 | 0% / 0% | PASS |
| 30 | 30.033 | 29.797 | 227.330/250.689/255.471/265.213 | 255.552/265.272 | 0% / 0% | PASS |
| 40 | 40.033 | 39.720 | 229.722/253.463/258.858/272.428 | 258.927/272.492 | 0% / 0% | PASS |
| 50 | 50.033 | 49.650 | 230.765/258.588/270.976/349.578 | 271.049/349.657 | 0% / 0% | PASS |
| 60 | 60.033 | 59.574 | 237.827/268.067/283.167/346.075 | 283.242/346.120 | 0% / 0% | PASS |
| 70 | 70.033 | 69.444 | 238.491/301.039/341.897/437.267 | 342.168/443.482 | 0.095% / 0.095% | FAIL |
| 80 | 80.033 | 79.385 | 252.452/351.609/381.969/482.303 | 390.269/710.378 | 0.990% / 1.000% | FAIL |

Therefore strict SLO capacity is `60 actions/s`; maximum observed stable
goodput is `79.385 actions/s` at the 80 phase, which is reported separately
because that phase violates the SLO. The knee/shedding onset is 70: it is the
first phase with 503s, shedding and retries. At 80, p50 also exceeds the SLO
and logical p99 rises to `710.378 ms`. CPU rose from `61.82%` to `99.17%`
per core across the ramp, while PostgreSQL connections stayed at `25`, waits
peaked at `2` and lock waiters stayed at `0`; CPU corroborates the knee but is
not the decision criterion.

The exact same ramp with the candidate `ready-vote-adaptive-v2` was run in
`33337425315` after deploy `33337237276` (source `094519c7`). Its strict SLO
capacity was also only 60 actions/s, and its maximum observed goodput was
lower, `73.528 actions/s`. It first shed at 70 (`1.083%`, retry
amplification `1.095%`), then at 80 reached `32.042%` shedding, `38.75%`
retry amplification and `5.708%` final logical failures; logical p95/p99 was
`1607.448/1756.531 ms`. The controller correctly reacted to sustained CPU
pressure by reducing the per-worker limit from 8 to 4, but the result was not
a material improvement. Production was restored to static-8 by deploy
`33337943520`; adaptive-v2 is retained as an evaluated candidate, not the
runtime profile.

The spike run (`33338160338`, source `094519c7`) passed with static-8. Normal
→ burst → normal offered rates were `10.033 → 80.066 → 10.033` actions/s;
accepted p99 was `431.439 → 238.336 → 181.006 ms`, with zero retries,
shedding and final failures. CPU peaked at `88.50%/90.18%`, PostgreSQL
connections at `23`, and waiters/lock waiters at `2/0`. The normal-after phase
returned below the normal SLO and cleanup passed.

The 15k stress run (`33339421320`, source `e0d27295`) passed its stress
contract after the duplicate phase was corrected to accept explicit overload
503s while requiring every successful duplicate to be `changed=false`. The
primary phase had 15,000 logical actions, 10,749 successes, 4,251 final
overload failures, `66.216%` temporary shedding and `112.113%` retry
amplification; overall accepted p50/p90/p95/p99 was
`332.872/561.139/650.456/802.414 ms`. This is stress evidence, not a normal
traffic final-failure SLO. All successful primary changes were durable, all
successful duplicates were no-ops, unexpected statuses were zero, CPU peaked
at `100%/100%`, PostgreSQL connections at `24`, waiters/lock waiters at
`3/2`, and exact cleanup passed. The optional 20k stress profile was not run.

Every measured run completed the exact cleanup matrix: all fixture users,
tournaments, sessions and audit rows were removed, while the control account
was preserved. The capacity and stress runs included server pool samples;
SLO/spike runs had no selected server pool timing spans, so those spans remain
diagnostic-only and do not replace the complete client-side population
metrics. The retry policy remains bounded at two overload retries; no retry
increase or blind retry was introduced.

## Ready Vote saturation after the final FastAPI fast path (2026-08-31)

This is the controlled follow-up to baseline SHA
`6580f7bf5c02641a8ff607c35bcc050e24b1a50e`. Both baseline and candidate used
two API workers, two vCPUs, `ready-vote-static-8`, admission `8/8/8`, the same
PostgreSQL/Redis/DB-pool budgets and the same GitHub-hosted external runner.
The 15k stress result is retained as stress evidence only; it is not the
canonical saturation ceiling.

The baseline rate sweep was split across the retained
[`ready-vote-saturation-ramp-v1`](profiles/retained-v1/ready-vote-saturation-ramp-v1.json)
(`33368575458`) and the active refinement `ready-vote-saturation-ramp-v4`
(`33374294139`). The table is the phase evidence used for the envelope:

| Offered target | Actual offered | Goodput | Accepted p95/p99 ms | Logical p95/p99 ms | Shed / retry / final fail | Source |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 80 | 80.033 | 79.392 | 355.358/528.541 | 433.702/1061.840 | 3.583% / 3.500% / 0.208% | v1 |
| 90 | 90.033 | 89.555 | 255.418/324.256 | 256.458/327.976 | 0.148% / 0.148% / 0% | v1 |
| 100 | 100.033 | 99.469 | 286.216/360.248 | 302.499/588.383 | 1.153% / 1.167% / 0% | v1 |
| 110 | 110.033 | 106.289 | 335.207/417.300 | 707.241/1332.903 | 7.034% / 7.273% / 0.273% | v1 |
| 120 | 120.033 | 115.607 | 339.128/413.136 | 779.391/1547.376 | 11.371% / 11.639% / 1.056% | v1 |
| 125 | 125.033 | 116.327 | 475.436/583.190 | 1800.244/2003.147 | 28.569% / 34.880% / 3.653% | v4 |
| 130 | 130.033 | 114.469 | 490.933/610.145 | 1822.401/2019.541 | 30.587% / 35.974% / 5.615% | v4 |
| 135 | 135.032 | 102.569 | 559.898/651.012 | 1937.233/2083.696 | 55.224% / 83.852% / 17.679% | v4 |

The resulting envelope is: `HEALTHY <=70/s`; `SLO EDGE = 80/s`;
`CAPACITY KNEE = 90–110/s`; `SATURATION = 120–130/s`; and `OVERLOAD =
135/s`. Goodput stops materially increasing in the 120–130 band. The
canonical baseline maximum stable goodput is therefore reported as
`~116 actions/s`, not as the 15k stress goodput `94.409 actions/s` and not as
the noisy single-phase maximum. At the saturation band CPU reached
`~88–89%` per core average (100% maximum), API process CPU was about
`115%` aggregate, PostgreSQL about `29%` average, connections `42` maximum,
waiting backends `2`, lock waiters `1`, and peak observed query wait was
`188.73 ms`. This is CPU/backpressure saturation with bounded database
pressure, not a PostgreSQL pool ceiling.

The candidate A/B runs were `33379397589` (`capacity-ramp-v2`, 20–80/s),
`33381896491` (`saturation-ramp-v1`, 80–120/s), `33384057053` and
`33384821610` (`saturation-ramp-v4`, 120–135/s), and `33385667381`
(`saturation-ramp-v2`, 120–165/s), all from source SHA
`68eb3f421049f8135bdf3b72c723dc4d93c8f57f`. Candidate goodput was
`69.406/79.418` at 70/80/s and `89.507/99.385/109.336/119.120` at
90/100/110/120/s. The repeated v4 runs produced respectively
`114.887/115.406/126.900/118.158` and
`116.476/121.775/127.570/129.097` at 120/125/130/135/s. The v2 sweep
provided the conservative repeatable plateau: `114.535/117.243/114.502/109.275`
at 120/135/150/165/s. The isolated 126–129/s v4 observations are retained as
run evidence but are not called a stable ceiling because the two v4 runs and
the v2 contour do not reproduce that exact peak.

At 120/s candidate accepted p95/p99 was
`327.020/409.036 ms`, logical p95/p99 `431.467/815.235 ms`, shedding
`5.045%`, retries `5.167%`, final failure `0.139%`; CPU averaged
`75.10%/75.75%` per core, API process CPU `93.27%`, PostgreSQL `29.00%`,
connections `41`, waiting backends `1`, and lock waiters `0`. The improvement
is material at the high end but does not move the normal SLO contract: the
candidate SLO capacity remains `70/s`, and the knee remains approximately
`80/s`. For the final envelope, maximum stable candidate goodput is reported
as `~117 actions/s` from the v2 contour; the `119.120/s` v1 and `126–129/s`
v4 observations are not used to claim a larger stable ceiling. This is a
small throughput change within the broad run-to-run spread, while the CPU
compiler cost reduction is directly confirmed by cProfile.

The bounded before profile was `33376398557` on cprofile runtime, with
profiling overhead excluded from latency conclusions. The ranked CPU evidence
was:

1. SQLAlchemy PostgreSQL insert compilation/coercion: `coercions.expect`
   self CPU `3.742045 s`; the dialect `visit_insert` path used `0.481978 s`
   across `7,232` upsert calls (`~0.067 ms` self CPU per upsert).
2. Async/runtime transport and middleware: h11/socket/asyncio self CPU was
   the largest remaining wall-clock group under cProfile, but it is mostly
   I/O/scheduling and was not treated as an endpoint optimization target.
3. SQLAlchemy greenlet/session execution: `greenlet_spawn` self CPU
   `0.719933 s`; `AsyncSession.scalar` and the endpoint service were smaller
   than the statement compiler target. Response instrumentation measured
   response handling at about `0.008 ms`.

The only code change was a Ready Vote-specific cacheable SQLAlchemy
`TextClause` for the conditional upsert. It keeps `AsyncSession.scalar`, one
DB checkout, one transaction boundary, auth/preflight, conditional upsert,
commit/rollback, trigger-authoritative counters, and post-commit cache
invalidation unchanged. The regression test asserts that the shared upsert
statement has a cache key while retaining the `ON CONFLICT`, conditional
choice predicate and `RETURNING` contract.

The candidate after-profile was `33380666490`. `visit_insert` was absent from
both worker profiles; `upsert_deadlock_ready_vote` self CPU was only
`0.068245 s` aggregate in that diagnostic run. The remaining
`coercions.expect` self CPU was `2.489327 s`, attributable to the other
statement shapes (especially preflight/auth), not the removed dialect insert
compiler. The next bottleneck is therefore the shared request/middleware/
transport and preflight/session path, with h11 header normalization,
asyncio/HTTP scheduling, auth and preflight execution visible above the
endpoint-specific upsert. No primitive Core rewrite was justified: the
observed DB CPU, connection count and lock/pool pressure were bounded, and
the response path was negligible.

The first candidate `ready-vote-stress-15k-v2` run (`33382724584`) was not
called PASS: it had 17 external-runner status-0 network errors. Its server
statuses remained 200/503 and cleanup passed. The identical rerun
`33383445876` passed the stress contract: primary goodput `108.032/s`,
shedding `61.575%`, retry amplification `99.140%`, final logical failure
`23.480%`, accepted p95/p99 `409.872/552.084 ms`, CPU `93.03%/93.29%` per
core, PostgreSQL `26.43%`, connections `41`, waiting backends/lock waiters
`1/1`, and exact cleanup. This is controlled stress evidence, not a normal
traffic failure-rate target. Both stress runs removed all fixture rows and
preserved the control account.

## Read-mix stress evidence (2026-09-01)

The first valid production `read-mix-stress-v2` run after the fixture-name
collision fix was [workflow `33559300059`](https://github.com/StrayForest/old_sparky/actions/runs/33559300059),
source SHA `9c46f70f1d23aa84c4e1f66bdfdb9e617e767b61`. It used the canonical
fixture of 40 tournaments × 500 users: 20,000 authenticated virtual users,
128 HTTP workers and 10,000 conditional workspace refreshes. The external
runner issued 30,000 requests in 499.717 seconds:

- initial read mix: 20,000 requests — 10,000 workspace, 6,000 tournament
  detail, 2,000 `/users/me`, and 2,000 tournament-list reads;
- manual refresh: 10,000 conditional workspace requests — 9,998 `304`
  responses;
- overall raw HTTP p50/p95/p99: `2057/4674/6324 ms`;
- response statuses: `19,997 × 200`, `9,998 × 304`, `3 × 503`, and `2`
  missing-initial-ETag refreshes;
- retries and classified overload shedding: `0` and `0%`.

The stress decision was **`STRESS BEHAVIOR FAIL`**. The three `503` responses
were unexpected for this profile (one `/users/me`, two workspace reads), and
the two affected users could not perform their conditional refresh. Accepted
latency budgets and origin-safety checks passed, but the correctness and
unexpected-status checks did not.

Origin evidence identifies CPU/backpressure as the current bottleneck: both
API cores averaged about `99%` and reached `100%`; PostgreSQL reached `51`
connections, `6` waiting backends and `0` lock waiters. Diagnostic
`request_perf` samples recorded overall p95/p99 `4803/6475 ms`, average DB
time `351 ms`, and pool checkout p95/p99 `3490/5182 ms`. The largest sampled
route groups were workspace (`16,957` requests, average DB time `423 ms`),
slug detail (`4,223`, `173 ms`) and `/me` (`1,489`, `304 ms`). This is bounded
database pressure with CPU saturation and pool queueing, not a lock-wait
incident.

Exact cleanup passed: all `20,000` fixture users and `40` tournaments were
deleted, with zero remaining fixture users, tournaments, sessions or audit
rows; the control account was preserved. Earlier runs `33553459937` and
`33553627531` were setup failures and are not performance evidence.

This profile validates authenticated page/API reads and conditional ETag
reloads. It does not validate the create → join → Ready Check → bracket
lifecycle and does not replace the separate browser/grid gate.

## Read-mix stress baseline (2026-09-02)

The next canonical `read-mix-stress-v2` run was [workflow
`33582068816`](https://github.com/StrayForest/old_sparky/actions/runs/33582068816),
source SHA `eb2ade0c82c193d82bd3720c19d0e9f168fc2719`. It used the same
40-tournament × 500-user fixture: 20,000 authenticated users, 128 external
HTTP workers and 10,000 conditional workspace refreshes. The runner issued
30,000 requests in 784.547 seconds:

- raw HTTP p50/p95/p99: `3086/7112/10010 ms`;
- statuses: `19,954 × 200`, `9,943 × 304`, `79 × 503` and `24` transport/status-0 errors;
- API cores averaged `99.54%` and `99.30%`; PostgreSQL averaged `13.25%` CPU;
- PostgreSQL reached `55` connections and `19` waiting backends, with zero lock waiters;
- server `request_perf` p95/p99 was `6993/9897 ms`, with pool checkout p95/p99
  `5431/8153 ms`;
- exact cleanup passed: all 20,000 users and 40 tournaments were removed,
  with no remaining sessions or audit rows and the control account preserved.

The decision was **`STRESS BEHAVIOR FAIL`** because the profile allows only
`200/304` responses. The evidence points to API CPU saturation and database
pool queueing, not PostgreSQL CPU or lock contention. Cloudflare edge caching
was separately verified on the public catalog path; this run therefore does
not justify disabling Cloudflare or replacing it with an origin-only cache.

## Read-mix A/B after authenticated-read optimizations (2026-09-02)

The first candidate after the baseline was [workflow
`33585907064`](https://github.com/StrayForest/old_sparky/actions/runs/33585907064),
source SHA `11a39ffd6d87f026425519eb39c83d6deda1dbf5`. It used the same
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes. The runner issued all 30,000 requests
in 473.178 seconds:

- raw HTTP p50/p95/p99: `1936/3982/5396 ms` (baseline: `3086/7112/10010 ms`);
- status counts: `19,999 × 200`, `10,000 × 304`, `1 × 503`, and no transport/status-0 errors;
- API cores averaged `97.42%` and `97.66%`; PostgreSQL averaged `19.05%` CPU;
- PostgreSQL reached `51` connections and `10` waiting backends, with zero lock waiters;
- server `request_perf` p95/p99 was `3697/5143 ms`, with pool checkout p95/p99
  `2687/4274 ms` (baseline: `5431/8153 ms`);
- exact cleanup passed: all 20,000 users and 40 tournaments were removed,
  with no remaining sessions or audit rows and the control account preserved.

The stress decision remains **`STRESS BEHAVIOR FAIL`** because one `503` is
still outside the profile's `200/304` contract. The A/B result is materially
better: wall time fell by 39.7%, p95 by 44.0%, p99 by 46.0%, and unexpected
responses fell from 103 to 1. The remaining limit is API CPU saturation and
pool queueing; Redis and PostgreSQL are not resource ceilings. The candidate
removed per-request Redis client creation, combined the active Ready Check
workspace read, avoided an unnecessary initial public-list DB checkout, and
collapsed the current-user identity reads. A subsequent candidate removes a
duplicate organizer-profile lookup when the main tournament query already
returned an explicit missing avatar asset; it still needs a new production A/B
run before being treated as an accepted optimization.

## Read-mix A/B after duplicate avatar lookup removal (2026-09-02)

The next canonical run was [workflow
`33588749169`](https://github.com/StrayForest/old_sparky/actions/runs/33588749169),
source SHA `7425e72ef90934ea6bb8a57bbffeb53936475f66`. It used the same
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes. All 30,000 responses satisfied the
profile contract and exact cleanup passed:

- raw HTTP p50/p95/p99: `1792/3685/5050 ms`; wall time `432.030 s` and
  `69.44 req/s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with no 503 or transport errors;
- API cores averaged `98.57%` and `98.54%`; PostgreSQL averaged `18.95%` CPU;
- PostgreSQL reached `51` connections and `7` waiting backends, with zero lock
  waiters;
- server `request_perf` p95/p99 was `3603/4865 ms`, SQL/request `5.753`, and
  pool checkout p95/p99 `2668/3987 ms`;
- all 20,000 users and 40 tournaments were removed, with no remaining
  sessions or audit rows and the control account preserved.

The stress decision was **`STRESS BEHAVIOR PASS`**. Compared with the prior
A/B, wall time improved by 8.7%, p95 by 7.5%, p99 by 6.4%, and workspace SQL
fell from `8.012` to `7.012` per request. The API remains CPU-bound at roughly
98.5%; PostgreSQL CPU, locks and Redis latency remain below their ceilings.
The next reviewed candidate added a narrow conditional-detail fast path, but it
was not accepted: the follow-up canonical run below was slower and returned
origin `503` responses. The candidate was rolled back, leaving this
`7425e72e` runtime as the accepted production baseline.

## Read-mix validation after conditional-detail fast path (2026-09-02)

The follow-up canonical run was [workflow
`33591512977`](https://github.com/StrayForest/old_sparky/actions/runs/33591512977),
source SHA `df277f0c0bad09d32e59f9b0b5ff0af5791bb002`. It used the same
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes. The exact cleanup and origin-safety
checks passed, but the stress contract did not:

- raw HTTP p50/p95/p99: `1973/4232/5893 ms`; wall time `480.806 s`;
- statuses: `19,995 × 200`, `9,997 × 304`, `5 × 503` and `3` derived missing-ETag
  refreshes;
- all five `503` responses occurred in the initial read phase and coincided
  with DB pool checkout reaching the 10-second timeout; the three missing ETags
  were downstream of those failed initial reads;
- API cores averaged `98.47%` and `98.35%`; PostgreSQL averaged `18.47%` CPU;
- PostgreSQL reached `51` connections and `4` waiting backends, with zero lock
  waiters; server pool checkout p95/p99 was `2988/4858 ms` and reached
  `10052 ms`;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

The decision was **`STRESS BEHAVIOR FAIL`**. Compared with the accepted prior
run, the result was slower and therefore does not establish a gain from the
conditional-detail fast path under this load. Cloudflare was separately
verified to serve the public catalog with `cf-cache-status: HIT`; it remains
enabled, with Redis as the origin read-model fallback. The next bounded
candidate samples successful slow-request diagnostic logs at `0.25` in
production; 5xx diagnostics remain fully logged. This changes observability
overhead only and does not change authorization, ETag inputs or response
business rules.

## Read-mix A/B after diagnostic-log sampling (2026-09-02)

The sampling candidate was validated by [workflow
`33594516140`](https://github.com/StrayForest/old_sparky/actions/runs/33594516140),
source SHA `3a8363f0c8874757eb56c9b9db5595e0b4043fbd`. It used the canonical
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes. The profile contract and exact cleanup
both passed:

- raw HTTP p50/p95/p99: `1890/3752/5324 ms`; wall time `454.799 s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with zero errors and unexpected
  statuses;
- API cores averaged `98.72%` and `98.79%`, each reaching `100%`; PostgreSQL
  averaged `18.97%` CPU and reached `51` connections;
- PostgreSQL waiting backends peaked at `6`, lock waiters and ungranted locks
  stayed at `0`; server pool checkout p95/p99 was `2685/4147 ms`, with no
  checkout timeout or `503`;
- successful diagnostic records fell from `27,472` in the prior run to `6,685`
  under the `0.25` sample rate; 5xx logging remains unconditional;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

Sampling removed diagnostic-log volume but did not produce a reproducible
latency gain versus the accepted `33588749169` run: wall time and p95/p99 were
slightly higher in this sample. It was rolled back together with the
conditional-detail fast path. The durable conclusion is that the two-vCPU
API is CPU-bound: PostgreSQL CPU, Redis latency and lock contention remain
below their ceilings, while API CPU stays near 100% and pool wait is the
resulting queue. No worker/pool increase or cache rewrite is justified without
new profiling evidence; Cloudflare edge caching remains enabled and the Redis
read-model cache remains the origin fallback.

## Read-mix validation after rollback (historical checkpoint, 2026-09-02)

This was the post-rollback checkpoint before the next workspace read-path
candidate. The deployed source was
`ffe74e8c932b9f1fbe0c3c25d8cf1fd5207058c6`, whose effective runtime was the
`7425e72ef90934ea6bb8a57bbffeb53936475f66` implementation plus rollback
commits. It retained the authenticated tournament read-path optimizations and
duplicate organizer-avatar lookup removal; the conditional-detail fast path
and successful-log sampling were not present.

Post-rollback [workflow `33602219984`](https://github.com/StrayForest/old_sparky/actions/runs/33602219984)
ran the canonical 20,000-user, 40-tournament × 500-user fixture with 128
external workers and 10,000 conditional refreshes. It passed the stress
contract and exact cleanup:

- raw HTTP p50/p95/p99: `1967/4015/5363 ms`; wall time `444.082 s` and
  `67.555 req/s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with zero errors and unexpected
  statuses;
- API cores averaged `98.43%` and `98.37%`, each reaching `100%`; PostgreSQL
  averaged `18.93%` CPU and reached `52` connections;
- PostgreSQL waiting backends peaked at `6`, lock waiters and ungranted locks
  stayed at `0`; server `request_perf` p95/p99 was `4017/5370 ms`;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

The result was **`STRESS BEHAVIOR PASS`**. The historical run
`33588749169` remains the fastest measured sample for that runtime; the fresh
run was slower by `2.8%` wall time, but had the same contract and resource-
safety outcome.

## Read-mix A/B after combined workspace base/access preflight (2026-09-02)

The next candidate was deployed through [production deploy
`33618864270`](https://github.com/StrayForest/old_sparky/actions/runs/33618864270),
source SHA `227a076bac3a3cfd42e8c62901ce2a29928ddd52`. It combines the
tournament base row and authenticated viewer access/commitment lookup into one
cached SQL statement for requests without an invite code, while retaining the
revision-based conditional 304 preflight from `829ccc2b`. The canonical
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes were measured by [workflow
`33619193304`](https://github.com/StrayForest/old_sparky/actions/runs/33619193304).
The profile contract, origin-safety checks and exact cleanup all passed:

- raw HTTP p50/p95/p99: `1341/2908/4414 ms`; wall time `353.045 s` and
  `84.975 req/s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with zero errors, retries,
  shedding or unexpected statuses;
- API cores averaged `98.36%` and `98.34%`; PostgreSQL averaged `21.49%`
  CPU and reached `41.28%` at peak;
- PostgreSQL waiting backends peaked at `9`, while lock waiters and ungranted
  locks stayed at `0`; workspace server pool checkout p95/p99 was
  `2068/3458 ms`;
- workspace request diagnostics fell from `6.018` to `4.501` SQL/request and
  from `438.892` to `278.235 ms` average DB time versus baseline run
  `33609896507`; the workspace pool checkout p95 fell from `3557` to `2068 ms`;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

The result was **`STRESS BEHAVIOR PASS`**. Wall time improved by `26.1%`
against the prior baseline and by `15.2%` against the immediately preceding
conditional-304 candidate (`33614703285`). The initial read phase improved
from `1849 ms` to `1576 ms` average versus the baseline; conditional refresh
latency remained within the same range (`1342 ms` in this run). This remains
the previous accepted workspace baseline for the next A/B below. The large
gain was consistent with removing one database checkout from authenticated
initial workspace reads; API CPU remained the limiting resource, while
PostgreSQL and Redis stayed below their resource ceilings.

## Read-mix A/B after combined workspace base/access/Ready Check preflight (2026-09-02)

The next candidate was deployed through [production deploy
`33624720919`](https://github.com/StrayForest/old_sparky/actions/runs/33624720919),
source SHA `4be82f1a9e682fda8bee990667b962f1f46e0b58`. It extends the cached
workspace base/access preflight with the selected Ready Check round, its
counter-shard common counts and the requesting user's vote overlay. The
workspace response contract is unchanged; the direct Ready Check endpoint,
vote mutation path and workflow writers remain authoritative and unchanged.
There is no schema or migration change. The canonical 20,000-user,
40-tournament × 500-user fixture, 128 external HTTP workers and 10,000
conditional workspace refreshes were measured by [workflow
`33625164162`](https://github.com/StrayForest/old_sparky/actions/runs/33625164162).
The profile contract, origin-safety checks and exact cleanup all passed:

- raw HTTP p50/p95/p99: `1212/2382/3492 ms`; wall time `301.757 s` and
  `99.418 req/s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with zero errors, retries,
  shedding or unexpected statuses;
- API cores averaged `98.40%` and `98.58%`; PostgreSQL averaged `23.21%`
  CPU and reached `46.76%` at peak;
- PostgreSQL waiting backends peaked at `4`, while lock waiters and ungranted
  locks stayed at `0`; workspace server pool checkout p95/p99 was
  `1659/2656 ms`;
- workspace request diagnostics fell from `4.501` to `4.018` SQL/request and
  from `278.235` to `228.691 ms` average DB time versus the previous accepted
  baseline; workspace pool checkout p95 fell from `2068` to `1659 ms`;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

The result is **`STRESS BEHAVIOR PASS`**. Wall time improved by `14.5%`
against the previous accepted workspace baseline (`353.045 s` → `301.757 s`),
and throughput increased by `17.0%` (`84.975` → `99.418 req/s`). The initial
read phase in this run had average/p50/p95/p99 `1319/1253/2731/3720 ms`,
versus `1576/1364/3620/4668 ms` in the preceding candidate. The conditional
refresh sample was also faster (`1208 ms` average), but this candidate does
not claim that change because matching `304` requests use the existing
conditional preflight. The workspace Ready Check stage no longer performs a
separate read; its state is materialized from the combined preflight. API CPU
remains the limiting resource, while PostgreSQL and Redis remain below their
resource ceilings. Production now retains this combined
base/access/Ready-Check preflight plus the proven conditional 304 path.

## Rejected conditional workspace auth preflight (2026-09-02)

The next candidate, source SHA `6344168a6ca93d6d0665c7de9ada1fdd09efabf8`,
deferred the cached-session validation query for the exact conditional
workspace request shape and folded that validation into the existing
conditional workspace preflight. Authorization, session expiry/invalidation,
email-verification policy, ETag inputs and non-workspace routes were kept
unchanged. The candidate was deployed by [production deploy
`33647303565`](https://github.com/StrayForest/old_sparky/actions/runs/33647303565)
and measured twice against the canonical `read-mix-stress-v2` profile.

- [first run `33647757275`](https://github.com/StrayForest/old_sparky/actions/runs/33647757275): `STRESS BEHAVIOR FAIL`, with `296` client
  failures including one `522`; workspace diagnostics were `3.390`
  SQL/request and `222.722 ms` average DB time;
- [repeat `33650029282`](https://github.com/StrayForest/old_sparky/actions/runs/33650029282): `STRESS BEHAVIOR FAIL`, with `29,999/30,000`
  successful responses and one `URLError` (`status=0`); workspace
  diagnostics were `3.504` SQL/request and `203.693 ms` average DB time,
  while origin safety rejected PostgreSQL connections peaking at `55` against
  the `52` ceiling;
- both runs completed exact cleanup, but neither satisfied the full contract.

The experiment was therefore reverted by `38c06fda`; the known-good combined
base/access/Ready-Check preflight remains the production baseline. The lower
SQL and pool measurements are useful evidence for a future auth optimization,
but this implementation is not accepted without a contract-passing design.

## Canonical commands

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
