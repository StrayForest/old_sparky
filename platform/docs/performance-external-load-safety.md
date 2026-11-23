# External-load execution safety and evidence

- Status: Active reference
- Owner: Platform performance
- Last reviewed: 2026-11-23

## Runtime deadlines and kill boundary

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

## Fail-closed timing and observer evidence

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

## External-load memory and diagnostic bounds

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

## Evidence privacy boundary

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

The `/users/me` supplemental account read-model contributes low-cardinality
Redis GET, awaited builder wall-time, and Redis write wall-time events to the
same existing request-performance stream. Builder time is elapsed wall time,
not CPU time; those spans do not cover route SQL, the full request, or DB
connection hold. Bounded outcomes distinguish hit, miss, read/write errors,
and build errors. Error outcomes use the existing error-retention trigger;
normal hit/miss records follow the process sampling policy. No account IDs,
cache keys, payloads, revisions, or timestamps enter these events.

Transport failures expose fixed `transport_error_code` counts over the full
failed-request population; capped example rows may carry the same enum. This
count is independent of the example cap. A completed HTTP error response (for
example, 502) remains a status failure and is not classified as a transport
exception. Older reports may omit the optional transport-code map. The
sanitizer retains only the fixed enum and never publishes exception text,
headers, or the internal SSR response ID. The existing SSR capacity diagnostic
opts workloads `authenticated_html_ssr_capacity_v1` (20-second HTML),
`authenticated_html_ssr_capacity_120s_v1` (120-second HTML), and
`authenticated_html_ssr_cpu_profile_20s_v1` (20-second CPU) into timestamp
capture; on those paths a validated response ID survives a body-read failure
for the existing source-bound origin join. The ordinary
`authenticated-page-load-v2` profile provides the transport-error census but
does not promise response-ID joins. Failures before response headers have no
response ID and remain uncorrelated.

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
