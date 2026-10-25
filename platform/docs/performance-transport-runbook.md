# Authenticated HTML transport runbook

- Status: Active operator procedure
- Owner: Platform maintainers

## CPU and memory diagnostic interpretation

The opt-in paired CPU diagnostic uses an eight-actor authenticated read cohort
and reports one bounded Node/V8 sample at a nominal four-second interval,
capped at five per phase. A delayed/incomplete window may contain fewer
samples; only exactly five accepted samples are complete, and missing samples
are never filled or promoted to PASS. RSS, V8 heap, external memory and array buffers are byte gauges; Node
reports `arrayBuffers` as a component of `external`, so do not add them
together. GC entries belong to a phase by their monotonic start time, and a
collection extending past the phase end contributes only its in-phase
duration. `sampled_window_duration_ms` is the interval between the first and
last accepted samples (normally 16 seconds), not the full 20-second request
phase. `memory_events_*_sampled_delta` cover only that sampled interval.
`memory.peak` is the cgroup lifetime high-water mark, while
`current_sampled_max` is the greatest sampled `memory.current` value; neither
is a continuous phase maximum. Missing cgroup support is UNKNOWN; malformed
data where the v2 hierarchy is present is INVALID. These diagnostics explain
the measured small workload and profiler overhead; they do not alone establish
the cause of an earlier OOM or the server's total capacity.

Load profiles must also state their arrival model. A closed-loop concurrency
test produces the rate the system can sustain and is not fixed-RPS evidence;
an open arrival schedule can reveal under-delivery and dropped work. Preserve
both scheduled and achieved request counts, generator quota/headroom and queue
lag before making a server-capacity claim. Capacity attribution treats p99
executor-queue wait above the protocol's 2 ms diagnostic floor as material;
open-loop runs also apply that floor to p99 scheduled-to-start delay. This floor
classifies generator evidence only: it does not change an SLO, latency budget,
or raw timing series. Any positive timer jitter remains visible, and closed-loop
schedule delay is not counted as missed offered rate because that model refills
after completion. See the primary
[`k6 open and closed models`](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/),
[`k6 dropped iterations`](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/dropped-iterations/),
and [Locust wait-time documentation](https://docs.locust.io/en/stable/writing-a-locustfile.html#wait-time).
Node metric semantics are defined by its [`process.memoryUsage()`](https://nodejs.org/api/process.html#processmemoryusage),
[`v8.getHeapStatistics()`](https://nodejs.org/api/v8.html#v8getheapstatistics),
and [GC performance entries](https://nodejs.org/api/perf_hooks.html#garbage-collection-gc-details).
`capacity_attribution_eligible` means only that the measured generator had
headroom and delivered the scheduled work for SUT-side analysis. It is not a
server-capacity claim. `server_capacity_claim` remains unknown with reason
`resource_correlation_not_provided` until production resource saturation is
independently correlated with the delivered workload; the public sanitizer
recomputes generator eligibility and does not trust an incoming claim. Older
reports without this distinction are never promoted to eligible.
The external-load supervisor may claim only GitHub-hosted VM-visible CPU quota
and effective cpuset when the exact runner PID/starttime, outer PID/mount/
cgroup namespaces, global cgroup2 mount, membership, every visible ancestor,
and sampled counters remain bound and complete. This does not establish
physical-host capacity or hypervisor scheduling headroom; missing or changed
evidence stays UNKNOWN. The hosted-runner premise follows the
[GitHub-hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
and [container-job boundary](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/run-jobs-in-a-container),
and quota/cpuset semantics follow the [Linux cgroup v2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html).

The separate `authenticated_workspace_read_capacity_v1` diagnostic is a
best-effort closed-loop SSR read workload with zero load-matrix credit. Its
small physical fixture has one tournament and 14 QA-contract users; 14 is an
existing diagnostic fixture minimum, not an API or business minimum. It uses
eight actors and a typed preset of 8 or 16 concurrent reads for one 20-second
ON window; the workflow defaults to 8. The OFF and ON windows also carry
process-wide worker CPU aggregates, aligned to their planned 20-second phase
boundaries. Missing or misaligned CPU samples leave generator attribution
unknown; measured CPU and cgroup capacity are evidence, not a pass threshold or
a server-capacity claim. The 16 preset remains a separate diagnostic run whose
memory margin and generator evidence require independent review. The
untraced warmup and OFF marker burst are separate from ON request counts, use
the same persistent HTTP/1.1 connection pool as the ON window, and are aligned
to the bound plan's wall-clock boundaries. The OFF burst must reach both API
worker processes; partial worker coverage remains incomplete. Server Node/V8,
GC and cgroup rows must survive the plan cleanup, dispatcher projection,
validator and capacity evaluator with the same run binding. The concurrent
read-only memory guard stops new submissions and drains in-flight work at its
predeclared safety boundary (768 MiB current memory for the bound 1 GiB web
cgroup, or any positive memory event); an abort is an incomplete diagnostic,
never a performance pass. These measurements can support workload-specific analysis,
but do not claim the server's maximum capacity or physical-host headroom.

The separately typed `authenticated_html_ssr_capacity_v1` workload measures
the authenticated `GET /tournaments/<fixture-slug>` document over pinned
HTTP/1.1 keep-alive. It reuses the existing capacity runner and page route;
its distinct contract requires authenticated ready-page semantics. The legacy
workspace workload uses eight actors at concurrency 8 or 16, while this
workload cycles a fixed cohort of 14 actors, so do not compare throughput
between these workload IDs. A
runtime-profile A/B must keep this HTML workload, source, fixture, observer
settings, and concurrency fixed and change only the runtime profile. Arm A is
`authenticated-read-admission-24x8` (`fetch`); arm B is
`authenticated-read-admission-24x8-native` (`node`). Both preserve the 24/8/
250 ms admission tuple and the same worker/pool settings. Before A1, verify
and record one web worker, compression enabled, API worker/pool values, and
the exact active profile; verify these values remain fixed after each switch.
A
completed response must be HTTP 200, `text/html`, and
contain the existing `tournament-detail-lifecycle` marker and ready-view
`top-detail-grid` marker, plus the existing server-rendered
`header-profile-avatar` marker. The lifecycle marker alone can also appear on
loading or invite HTML; the detail grid alone can also appear for an anonymous
viewer of a public tournament. Requiring all three rejects those shells. The
check retains only booleans and never records identity text. A bad status,
content type, marker, memory guard, restart, or unknown watcher stops new
submissions; in-flight requests drain under the existing timeout, partial
counts remain visible, and every credit stays false. Response bodies are read
within the existing byte bound and are never retained in reports. This typed
mode uses 14 fixture actors cyclically. Presets 8 and 16 are the initial
steps; 32 and 64 are available only to this HTML workload and deliberately
reuse those 14 actors, so an actor may have multiple concurrent requests.
That closed-loop workload has no fixed offered arrival rate and is not
equivalent to the 20,000-user authenticated-page-load profile. Treat each
concurrency as a distinct diagnostic step. Step 8, 16, 32, then 64, stopping
when admitted request count per nominal 20-second window plateaus or a safety
guard fires. `submitted` counts executor submissions while that window is open;
the runner drains in-flight requests afterward, so `completed` can include
post-window responses. `observed_duration_ms` includes that drain and is not a
20-second completion-throughput denominator. The report has no count of
responses completed before the window edge; do not label `completed / 20s` as
in-window throughput. Compare fetch/native arms at
the same selected concurrency with the same final app tree and observer
settings in A/B/A order; if results are close or noisy, repeat B before
selection. Keep a runtime candidate only for repeatable throughput or tail
latency improvement with no semantic, error, guard, or restart regression.
Baseline drift makes the comparison inconclusive. Without complete aligned
CPU and runner-headroom evidence, do not infer a hardware ceiling from
throughput. Both OFF and ON windows carry the same SSR request-stage
traces, per-process CPU usage windows, and Node memory/GC sampling; they do not
enable cProfile (reserved for the separate CPU-pair workload). Compare only
runs with this same observer configuration, and do not add nested SSR spans as
if they were disjoint durations. Observer overhead is not isolated by this
diagnostic, so its HTTP rate and latency are instrumented results rather than
an estimate of an uninstrumented production ceiling.

Any incomplete parent outcome is surfaced as a bounded JSON job-log fingerprint
with namespace closure, worker/runner states, allowlisted runner and supervisor
reasons, a closed startup-failure stage when available, process lifecycle
booleans, bounded exit status and plan cleanup state;
these fields never award credit. The sanitizer keeps the report
projector strict. If projection fails, its invalid marker reports only a closed
stage/reason, section/field label, type and presence bit, with all credits false;
it does not echo report values, paths, raw exception text or unexpected keys.
An early runner-sampler stop uses the complete all-false namespace identity map
and remains UNKNOWN, so its failure report can pass structural projection but
still cannot be promoted to capacity evidence.
Namespace exit 125 diagnostics use only fixed stage bytes across the watchdog
pipe and namespace READY stream. The watchdog descriptor is close-on-exec before
the system chain, and the inner helper's duplicated stream is close-on-exec
before the worker; neither descriptor reaches sudo or the worker. Failure
fingerprints preserve the aggregate stage and separately identify the
pre-READY namespace stage, post-READY namespace stage, and watchdog stage.
This keeps a secondary cleanup failure from erasing the bounded startup source;
the aggregate remains `unknown` if sources conflict. A valid watchdog pidfd
cleanup code may coexist with one startup stage, while duplicate, malformed or
oversized channel data remains unknown. `supervisor_process_started` means only
that the watchdog process was launched; it does not mean the inner worker
started. These diagnostic fields never change containment status or award
capacity credit.

Use this procedure to separate authenticated HTML latency across direct
Next.js, local Nginx and public Cloudflare. It is diagnostic evidence only; the
canonical external load profile remains the authority for production latency,
correctness, origin safety and cleanup.

## Preconditions

Record the deployed source/release, `nginx -v`, `nginx -t`, the diagnostic
profile state and the exact page path. Use a disposable authenticated test
account and a private one-line Cookie-header file with mode `0600`. Never put a
cookie in shell history, command output, an artifact or a report.

The source Nginx policy disables response buffering only in the dynamic HTML
proxy location and emits `X-Accel-Buffering: no`. API and static locations keep
their existing buffering/cache contracts. The JSON access log records upstream
and client encoding, transfer and buffering headers.

## Correlated SSR and auth/API diagnostic

The `web-ssr-diagnostics` profile enables the existing sampled SSR trace and a
separate API log gate. It also selects the public `INFO` log level and global
performance logging flag in the API environment, so fast diagnostic rows are
not hidden by stale startup configuration. For the same sampled requests, Next.js sends
`x-platform-ssr-trace: 1` with explicit diagnostic request and Cloudflare
correlation headers to `GET /api/v1/auth/bootstrap`; the API uses those headers
only for the marked diagnostic hop. The API then emits its existing bounded
`request_perf` record even when the request is faster than the normal slow
request threshold. The public timeout diagnostic marker is bounded, passive
metadata in the Nginx edge access record only; it never enters SSR/API tracing,
changes SSR sampling or forces an API request log. The separately named timeout-path window keeps
SSR/event-loop diagnostics and API request logging off; its required evidence
is the bounded client-timeout to Nginx route/status/upstream-timing join.
Applying the profile restarts both
`deadlock-api` and `deadlock-web` with readiness checks because the API gate is read at process
startup. Before applying it, record the exact prior runtime profile and verify
that its web-worker and auth-transport values match the diagnostic profile's
one-worker/fetch defaults. Restore the recorded profile through the supported
runtime-profile workflow; do not assume `ready-vote-static-8` is the prior
profile or edit the shared environment directly. When the prior profile is
`authenticated-read-admission-24x8`, use the supported baseline-then-prior
sequence, checking readiness after each transition. The 24x8 restore selector explicitly resets
`PLATFORM_SSR_WORKSPACE_ERROR_DIAGNOSTIC=false` while preserving the 24/8/250ms
admission tuple and existing API/database pool settings.
The API only accepts internal SSR diagnostic headers from the loopback SSR
hop; every public API proxy location clears those headers. The API auth/bootstrap gate
also requires the expected route, method, internal trace marker and startup
profile flag.

The production observer may use request-local correlation internally while it
is reducing the journal window, but it never persists the request ID or
Cloudflare ray. The report exposes only aggregate join counts and the join
method class. Read
`correlated_html.timeline[].api_request_perf[]` together with the SSR stages:

The profile also enables the bounded `ssr_error` tuple for sampled workspace
API failures. The observer joins these rows to Nginx only by a unique request
ID and publishes stage, error family, status and response-code counts; request
IDs, Cloudflare rays and diagnostic run IDs remain private and transient. This
is a sampled error population: zero observed rows does not establish that no
unsampled errors occurred. Restore the prior runtime profile after the window;
the baseline sets `PLATFORM_SSR_WORKSPACE_ERROR_DIAGNOSTIC=false`.

The aggregate Nginx HTML observer includes exact `GET /tournaments/<slug>`
records with client statuses from 100 through 599, so failed responses remain
visible in the request/status and timing summaries. `upstream_statuses` counts
upstream attempts, which can exceed client requests when Nginx retries; a dash
attempt is counted separately as unknown. These are separate distributions:
compare client status counts to load results and upstream-attempt counts to
diagnose origin retries. The projection retains no slug, query, or raw access
log value.

- `total_ms`/`request_ms`, `sql_ms`, pool wait and the
  `auth_bootstrap_*_ms` fields measure auth/API work inside FastAPI.
- `auth_bootstrap_fetch`, `root_layout` and `first_body_write_attempt` measure
  the surrounding Node SSR path. Compare their gaps with event-loop p95/p99,
  ELU, process CPU and GC duration to identify scheduling/queueing.

Sampled authenticated-page traces also expose bounded inner spans:
`internal_read_admission_wait` covers the process-local SSR read permit wait;
`auth_http_fetch`/`workspace_http_fetch` cover the request until a response is
available; `auth_body_parse`/`workspace_body_parse` cover body consumption and
JSON parsing;
and `auth_projection`/`workspace_projection` cover validation and mapping into
web types. Queue failures are marked `outcome=error`; an immediate admission
is a measured zero/near-zero wait. These names are closed by the evidence
sanitizer and the spans contain no route, slug, cookie, body or identity data.
They are nested inside the existing outer `auth_bootstrap_fetch`,
`auth_current_user_fetch` and `tournament_workspace` spans, so do not add their
durations as if they were disjoint end-to-end components. With the native Node
auth transport, `auth_http_fetch` includes request, body collection and eager
JSON parsing; `auth_body_parse` is a separate parser-local timer nested inside
that total and overlaps it. This eager parse on every status preserves the
existing malformed-body behavior. With fetch transport, `auth_http_fetch`
ends when `fetch` returns its response, while `auth_body_parse` includes body
consumption and JSON parsing. Compare only like transport profiles. All spans
remain disabled with the SSR diagnostic flag off; the bounded capacity plan
may still explicitly enable its request-local trace.

Run this only as one explicitly reviewed `web-ssr-diagnostics` window using the
canonical external authenticated-page profile. It leaves compression,
database pools, API admission and capacity limits unchanged; its selected web
worker and auth-transport values are one and `fetch`, so verify those match the
recorded prior profile before applying it. If the observer
records a web restart or any unexpected status, treat the window as diagnostic
evidence rather than a clean latency result and follow [Web restart evidence](#web-restart-evidence).
Restore the exact recorded prior profile after the window so the diagnostic
keys and API logging selectors return to their baseline values
(`PLATFORM_SSR_PERF_LOG_ENABLED=false`,
`PLATFORM_PERF_AUTH_BOOTSTRAP_LOG_ENABLED=false`, `PLATFORM_LOG_LEVEL=INFO` and
`PLATFORM_PERF_LOG_ENABLED=true`).

The server-side SSR read scheduler bounds internal read-only workspace,
bootstrap and current-user requests per web process: 24 active operations,
128 FIFO waiters and a five-second queue deadline. It holds a permit through
response-body parsing or cancellation and releases it on success, error or
abort. Queue saturation does not retry an API request or change browser
requests, authentication, API admission, database pools or write paths. This
is a request-fanout guard, not a capacity increase; validate its effect only
with the complete external-load profile matrix.

`server_request_perf_logs` reports
`authenticated_read_admission_shed.{observed,shed,not_shed,unknown}` globally
and for each safe route class. These are counters from the selected
`request_perf` journal population, not all HTTP requests. Missing or malformed
boolean values count as unknown, and sampled counts must not be extrapolated
to the full client population.

The separate `PLATFORM_SSR_HEAP_METRICS_ENABLED` selector is false in the
public baseline and every current managed runtime profile. The deploy
supervisor explicitly selects this key on each profile application, and the
shared-env profile defaults it to `false`; applying or restoring a profile
therefore leaves no heap-only sampler running. The service-env renderer keeps
the selector in `web.env` and omits it from `api.env`. If a future reviewed
profile enables it, use a separately documented memory-only window and do not
overlap it with request-level SSR or other profiling. Missing `ssr_heap` rows
in the current profiles mean the sampler is off, not that a collection failed.

## Timeout-path diagnostic window

Use this narrow window when the unchanged
`authenticated-page-load-v1` baseline has client `TimeoutError` results but
no 502/OOM evidence. It is a cause-localization run, not a clean baseline,
performance result or optimization A/B. Keep the load contract unchanged
(20,000 users, 40 tournaments, HTTP concurrency 64, no retries) and do not
change workers, pools, admission, timeout values, auth/cache/security
semantics, Nginx/Cloudflare behavior or application logic.

The timeout contour uses bounded observation order rather than persisting a
request, diagnostic, correlation, Cloudflare-ray or user identifier. The
client timeout population is joined to Nginx's allowlisted route class,
upstream status/timings and numeric system/CPU/PostgreSQL aggregates. The
first, low-overhead contour intentionally leaves SSR/event-loop and API
request logging off; an absent SSR/API row therefore means “not instrumented”,
not “not called”. The optional full diagnostic profile enables the normal
bounded request sample for SSR/API lifecycle evidence and event-loop samples,
but must be treated as a diagnostic-pressure window if it creates restarts or
unexpected statuses. A caller-provided timeout marker cannot enter or promote
an SSR/API trace; normal trace selection uses the Nginx request ID or
Cloudflare ray. Reports retain only `observation_index`, route class, status, error
class, completion booleans, bounded stage counts and timings; unknown routes
map to `other`.

Run sequencing is deliberately three-step:

1. Record the current release, runtime profile and `MemoryMax`; keep the
   pre-window profile active for the minimal contour. Run the external workflow
   with the dedicated timeout confirmation:

   ```bash
   gh workflow run platform-production-external-load.yml \
     --repo StrayForest/old_sparky --ref dev \
     -f confirmation=RUN-PRODUCTION-TIMEOUT-DIAGNOSTICS \
     -f control_email=<existing-production-account-email> \
     -f profile_id=authenticated-page-load-v1 \
     -f timeout_diagnostics=true
   gh run watch <diagnostic-run-id> --repo StrayForest/old_sparky --exit-status
   ```

   Do not enable `web-ssr-diagnostics` for this first pass: its marked-request
   trace adds per-request SSR/API/event-loop logging and can change the failure
   mode. If the minimal contour leaves an origin-localized timeout unresolved,
   run the full profile as a separately identified diagnostic window, then
   restore the pre-window profile.
2. Read the bounded `timeout-diagnostics.json` rows by observation index.
   Missing Nginx evidence means only “not observed at origin” (client vs
   Cloudflare is unresolved), not proof that an edge layer was healthy. API
   start without completion means the API accepted the call but completion was
   not observed. Because correlation identifiers and timestamps are not
   persisted, the report deliberately does not infer cross-clock ordering or
   claim an origin completion after the client timeout. Compare numeric rows
   with the nearest aggregate system sample; event-loop evidence is available
   only when the full diagnostic profile was active. Do not infer CPU
   saturation from aggregate CPU alone.
3. Restore the exact pre-window runtime profile through the production deploy
   workflow (substitute the recorded profile in the assignment below), for
   example:

   ```bash
   PRE_WINDOW_PROFILE="ready-vote-static-8"
   gh workflow run platform-production-deploy.yml \
     --repo StrayForest/old_sparky --ref dev \
     -f mode=deploy -f "runtime_profile=$PRE_WINDOW_PROFILE" \
     -f web_compression=enabled
   gh run watch <restore-run-id> --repo StrayForest/old_sparky --exit-status
   ```

   Verify both services are ready, confirm diagnostics are off and confirm
   `MemoryMax` is unchanged; this workflow must not be used to tune the
   memory ceiling. The supervisor always performs exact
   fixture cleanup; its temporary manifest and handoff are removed at the
   barrier. Do not delete the retained diagnostic artifact until the evidence
   review is complete, then apply the normal bounded storage-retention
   procedure.

Only after the evidence identifies a reversible bottleneck may an operator
propose a separate fix for approval. Do not rerun the full 20,000/20,000
acceptance baseline or the correlated performance run as part of this window.

## Explicit client transport A/B

Use this procedure when the question is whether the load generator's HTTP
connection behavior is contributing to the public authenticated HTML result.
The application and its production runtime remain unchanged. Run the canonical
v1 control and the v2 candidate from the same reviewed source SHA, with the
same `20,000` users, `40` tournaments, concurrency `64`, timeout and no-retry
contract. Require the v1 window to be clean before treating v2 as comparable.

The control is `authenticated-page-load-v1`; it retains the historical urllib
HTTP/1.1 connection-close client. The candidate is
`authenticated-page-load-v2`; it uses a bounded HTTP/1.1 keep-alive connection
per external-runner worker. Its report must show the selected transport,
HTTP-version counts, connection reuse/new counts and finite phase timings for
DNS, TCP, TLS, request write, header wait, TTFB, body receive and total time.
The candidate is not browser-equivalent and does not claim HTTP/2 or HTTP/3
behavior. Do not change the acceptance thresholds, auth cookies, retry policy,
origin routing or server runtime while making this comparison.

The current release toolchain is pinned to Node `26.3.1` by the build,
artifact and live-QA guards. A Node 24 versus Node 26 A/B therefore requires a
separately reviewed pinned Node 24 archive, provenance and release/preflight
support; do not bypass those guards or infer a runtime comparison from a local
unmanaged Node binary.

Run the two normal external-load workflow windows sequentially:

```bash
gh workflow run platform-production-external-load.yml \
  --repo StrayForest/old_sparky --ref dev \
  -f confirmation=RUN-PRODUCTION-EXTERNAL-LOAD \
  -f control_email=<existing-production-account-email> \
  -f profile_id=authenticated-page-load-v1 \
  -f timeout_diagnostics=false
gh run watch <v1-run-id> --repo StrayForest/old_sparky --exit-status

gh workflow run platform-production-external-load.yml \
  --repo StrayForest/old_sparky --ref dev \
  -f confirmation=RUN-PRODUCTION-EXTERNAL-LOAD \
  -f control_email=<existing-production-account-email> \
  -f profile_id=authenticated-page-load-v2 \
  -f timeout_diagnostics=false
gh run watch <v2-run-id> --repo StrayForest/old_sparky --exit-status
```

The workflow owns fixture creation and exact cleanup. If either window has
unexpected statuses, client errors, a web restart, incomplete observer data or
cleanup failure, retain it as diagnostic evidence and do not promote the
transport. Restore/verify `ready-vote-static-8` if an operator runtime profile
was used for any adjacent server-side A/B.

## Observed production window — 2026-09-11

The reviewed implementation was measured from source SHA
`0700b7402ecdd182fe0cfba4feae14f15fb68243` with the unchanged
`20,000 / 40 / c64 / 30s / no-retry` contract. Exact fixture cleanup removed
all `20,000` users and `40` tournaments after every window, and the final
storage-maintenance run verified backup restore and service health.

| Window | Runtime/client | HTTP result | Decision |
| --- | --- | ---: | --- |
| [`34542019146`](https://github.com/StrayForest/old_sparky/actions/runs/34542019146) | baseline / v1 connection-close | 18,077 200; 1,923 502; 9.615% failure | control failed stress acceptance |
| [`34543832582`](https://github.com/StrayForest/old_sparky/actions/runs/34543832582) | baseline / v2 HTTP/1.1 keep-alive | 15,938 200; 4,061 502; 1 no response | do not promote client transport |
| [`34545960967`](https://github.com/StrayForest/old_sparky/actions/runs/34545960967) | one worker / native loopback transport | 17,549 200; 2,451 502 | do not promote runtime profile |
| [`34548935025`](https://github.com/StrayForest/old_sparky/actions/runs/34548935025) | two workers / native loopback transport | 14,762 200; 5,238 502 | do not promote runtime profile |

The v2 report confirmed HTTP/1.1 reuse (`19,935` reused connections and `65`
new connections), so the client-side comparison was instrumented, but it did
not improve the public result. Every pressure window observed web-process
replacement with peak RSS near the `1 GiB` cgroup limit. PostgreSQL ownership,
lock-waiter and cleanup safety checks passed, so the evidence points to the
web memory/render-pressure path rather than a database ownership failure.
Production was restored to `ready-vote-static-8` with `fetch` authentication
and compression enabled by
[`34550534777`](https://github.com/StrayForest/old_sparky/actions/runs/34550534777),
followed by launch QA
[`34550849736`](https://github.com/StrayForest/old_sparky/actions/runs/34550849736)
and storage maintenance
[`34551221016`](https://github.com/StrayForest/old_sparky/actions/runs/34551221016).
The next optimization should target bounded HTML render/memory behavior; do
not change the memory ceiling, bypass the Node `26.3.1` release guard or
promote either experimental profile from this evidence.

## Same-request hop probe

Run from an operator host or the origin. The probe reads the cookie, measures
time to response headers and total body time, records selected transport
headers, and never prints the cookie or response body:

```bash
cd /opt/oldsparky/platform/current
python3 tools/platform_ttfb_probe.py \
  --cookie-file /run/oldsparky/qa-cookie \
  --hop next=http://127.0.0.1:3000/tournaments/<fixture-slug> \
  --hop nginx=https://127.0.0.1/tournaments/<fixture-slug> \
  --hop cloudflare=https://old-sparky.com/tournaments/<fixture-slug>
```

Compare `ttfb_ms`, `content-encoding`, `transfer-encoding`,
`x-accel-buffering`, `cf-cache-status` and the bounded Cloudflare error class.
The public hop is the visitor-facing evidence; raw request IDs and ray values
are not retained. Repeat the probe enough times to see variance; do not add
its single-request values to external-load p95s.

The external-load client reads each response body within its fixed byte cap.
Known socket, timeout and HTTP protocol/read exceptions become one failed
request with a closed transport class and bounded byte count; partial response
content is discarded, the request is not retried by this reader, and the phase
continues accounting later requests. Unexpected programming exceptions still
fail the worker. These transport failures remain errors and cannot contribute
successful-request latency or goodput.

## Compression A/B

Next compression is a build-time switch, not a runtime profile toggle. The
production deploy workflow exposes `web_compression=enabled|disabled`; it
defaults to `enabled`, passes the choice through the sanitized CI build
environment, and records the resulting choice in `RELEASE.json`. Keep the
default artifact compressed, then deploy a separately named candidate from the
same reviewed source SHA with `web_compression=disabled`:

```bash
gh workflow run platform-production-deploy.yml \
  --ref dev \
  -f mode=deploy \
  -f runtime_profile=ready-vote-static-8 \
  -f web_compression=disabled
```

Run the same control/candidate request shape and hop probe. Retain compression
unless an unchanged-window comparison shows a reproducible first-byte gain
without response-integrity or security regressions. Do not disable Nginx or
Cloudflare compression globally as a first response.

## Cloudflare evidence

Run the read-only Cloudflare audit workflow and inspect
`response-buffering-zone-setting` plus `response-body-buffering-rules`. The
first is the legacy Enterprise zone setting; the second reports per-request
Configuration Rules. Do not change either setting from this repository. A
`none` response-body rule is acceptable only after confirming that the scoped
authenticated HTML path does not require response-body inspection by WAF or
Bot Management.

## Web restart evidence

If an external-load observer records a missing/new `deadlock-web` process, do
not accept the latency result as a clean control or candidate. Dispatch the
read-only `Platform production web runtime diagnostics` workflow from `dev`
with the exact deployed SHA and the observer's UTC window:

```bash
gh workflow run platform-production-web-runtime-diagnostics.yml \
  --ref dev \
  -f expected_sha=<deployed-sha> \
  -f since_utc=2026-09-09T09:33:00Z \
  -f until_utc=2026-09-09T09:54:00Z
```

Inspect systemd exit/result, restart count, memory peak/current, the fixed
aggregate service/kernel journal summaries, Nginx service state and its bounded
error summary, plus any filesystem/inode/mount facts captured by a candidate
activation failure.
The diagnostics producer and both summary helpers are release-owned contracts:
an unavailable helper, service query or Nginx error log fails the workflow and
must be investigated as a collection failure. Only a successful producer with
no records may produce the fixed `empty` summary; no raw-log fallback is
permitted. Production SSH material is step-scoped and removed before the
aggregate artifact upload.
Do not raise `MemoryMax`, scale workers or alter
the database pool until the restart cause is identified and a focused rollback
plan exists. A web restart can create both 502s and secondary TTFB queueing.
