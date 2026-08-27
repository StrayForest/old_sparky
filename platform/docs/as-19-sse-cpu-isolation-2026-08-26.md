# AS-19 CPU-isolation round — 2026-08-26

## Scope and gate

This is the active measurement record for the public 1k → 5k → 10k SSE
staircase. Cloudflare, Nginx, Redis admission and the application global cap
remain enabled. A result is valid only when the intended stream hold/report
completes without a load-generator, probe-Redis, edge-queue or unexpected
application error. CPU from the QA process is not server CPU.

The supplied external review was also assessed. Its `HTTP pool 40/80/160`,
generator-queue timing, workspace phase timing, early-304 short circuit and
workspace representation-cache ideas belong to the combined polling branch,
not this SSE-only staircase. They remain follow-up measurements: the current
SSE 10k failure has no origin request-latency sample to attribute to Python
serialization or workspace construction. A sustained two-core CPU ceiling is
also a possible final result, but it must be shown with healthy API CPU,
origin latency, generator and Redis probe signals.

## Initial staircase

Deployed runtime: `955bee38`.

| Profile | Result | Interpretation |
| --- | --- | --- |
| 1,000 SSE, 3 events | 1,000/1,000 HTTP 200; 3,000/3,000 events; API CPU avg 43.45%; no sustained CPU saturation | Valid baseline; CPU is not the bottleneck. |
| 5,000 SSE, 3 events | 3,000 HTTP 200; 2,000 fast application `429`; 9,000/9,000 events; API CPU avg 45.44%; no sustained CPU saturation | Controlled degradation, not a 5k capacity pass. |
| 10,000 SSE, 3 events | No valid SSE summary; the load generator timed out opening Redis while publishing probe events | Invalid CPU measurement; QA CPU avg ~84%, API workers ~2–3%, Redis CPU low. |
| 10,000 SSE, 0 events | 5,875 HTTP 200; 4,125 public `429`; 0 errors/503; 0 events; API CPU avg 51.53%; sustained CPU false | C1 removed the probe failure, but edge admission/queue stopped the public test first. Nginx saw 200 upstreams only; p95 server route was ~598s. |

## Selected hypothesis

**C1 — Probe publication is a load-generator confounder at the 10k barrier.**

The failed run timed out in `publish_probe_events()` after stream setup. The
QA process, not the API workers or Redis server, held the CPU signal. First
remove probe publication to obtain a valid persistent-connection baseline.

C1 passed as a measurement correction, not as a 10k capacity result. Its five
follow-ups remain the active next matrix; the first product-side branch must
address public admission/pacing before any attempt to raise the app ceiling.

The first C10 pacing A/B is also rejected: `open_concurrency=64` produced
5,875 HTTP 200 / 4,125 edge 429 with connect p95 706s, while `16` produced
5,838 / 4,162 with connect p95 728s. Both had server-route p95 near 598s and
no sustained API CPU saturation. Slowing the opener does not remove the edge
queue.

## Ten CPU-focused hypotheses

| ID | Hypothesis | Evidence required |
| --- | --- | --- |
| C1 | `event_count=0` isolates persistent SSE admission/hold. | Valid 10k report without probe failure. |
| C2 | Pre-warm/reuse one Redis publisher client before the barrier. | No peak-time connect timeout; fewer new connections. |
| C3 | Serial channel publishing is safer than 20-way `gather`. | Same events, lower QA CPU, no storm. |
| C4 | A bounded Redis pipeline lowers round trips. | Lower publish latency without Redis errors/memory growth. |
| C5 | An explicit small publisher pool avoids hidden connection growth. | Stable clients and bounded wait. |
| C6 | One shared application Redis client per worker lowers publish churn. | Lower churn, correct shutdown. |
| C7 | Pre-encoded relay SSE envelopes lower API serialization CPU. | Lower API CPU at equal delivery. |
| C8 | Coalescing duplicate revisions lowers queue writes. | Same final revision, lower fan-out CPU. |
| C9 | A measured longer keepalive interval lowers idle CPU safely. | No idle disconnects; revalidation remains intact. |
| C10 | Change public admission/pacing in small steps only after C1. | More streams without edge/5xx/resource failure. |

## Five follow-ups around C1

| ID | Variant | Decision metric |
| --- | --- | --- |
| C1-F1 | 10k, zero events, 60s hold. | DONE: probe removed, edge 429 remained; no CPU saturation. |
| C1-F2 | 10k, one event/tournament, pre-warmed publisher. | Blocked until a public 10k connection baseline avoids edge queue. |
| C1-F3 | 10k, three events/1s, serial publisher. | Blocked until a valid baseline exists. |
| C1-F4 | 10k, three events in one bounded pipeline. | Blocked until a valid baseline exists. |
| C1-F5 | 10k, ten events/1s, bounded publisher concurrency. | Blocked until a valid baseline exists. |

Rank by zero unexpected public errors, accepted streams, complete events,
p95/p99 latency, API CPU/load, Redis counters, PostgreSQL pool/locks, then
throughput. Do not rank a high QA-process CPU result as server saturation.

## Research basis

- [Redis connection pools and multiplexing](https://redis.io/docs/latest/develop/clients/pools-and-muxing/)
  recommends reusing a small pool or a multiplexer rather than repeatedly
  opening connections.
- [Redis pipelining](https://redis.io/docs/latest/develop/using-commands/pipelining/)
  documents batching commands to reduce round trips and socket I/O, with
  bounded batch sizes to avoid reply-memory growth.
- [Redis async Python guidance](https://redis.io/docs/latest/develop/clients/redis-py/async/)
  recommends one shared client for a long-running async application.
- [FastAPI dependency scopes](https://fastapi.tiangolo.com/advanced/advanced-dependencies/)
  support closing short-lived yielded resources before a streaming response;
  authorization revalidation remains separate.
- [Gunicorn design](https://docs.gunicorn.org/en/stable/design.html) treats
  asynchronous workers as the appropriate model for streaming and long polls;
  worker count is not a client-count substitute.
- [Discord's relay/passive-connection report](https://discord.com/blog/maxjourney-pushing-discords-limits-with-a-million-plus-online-users-in-a-single-server)
  supports reducing fan-out, while also warning that extra process boundaries
  can add memory, GC and backpressure costs.
- [Nginx proxy documentation](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
  defines `proxy_read_timeout` as an idle interval, not a total stream limit.
- [Cloudflare Error 1200](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1200/)
  is an edge queue/admission signal and must not be mistaken for VPS CPU.

## Production fast-fallback A/B round

The production target is now **10,000 human-like virtual users**, not
10,000 simultaneous SSE handshakes. A visitor must never remain in an edge
queue for minutes. The browser opens SSE only while the handshake is healthy;
otherwise it closes the attempt and uses revision polling with conditional
ETags. The SSE timeout is a UX guard, not a Cloudflare bypass and not an
authorization relaxation.

The local candidate now uses a 1-second browser handshake timeout and a
60-second SSE retry cooldown. The retained-load runner accepts the same
timeout as an explicit A/B parameter and records timeout/fallback eligibility
separately from unexpected errors. The browser value is a bounded build-time
configuration named `NEXT_PUBLIC_PLATFORM_SSE_OPEN_TIMEOUT_MS`; when unset it
defaults to 1 second.

### Ten hypotheses

| ID | Hypothesis | Measurement |
| --- | --- | --- |
| P1 | 1-second SSE timeout gives the best perceived response. | Page fallback latency, successful SSE rate, polling request rate. |
| P2 | 3-second timeout keeps more healthy SSE while avoiding edge waits. | Same metrics; reject if fallback exceeds the UX budget. |
| P3 | 5-second timeout is the best balance for normal mobile/network variance. | Same metrics plus reconnect rate. |
| P4 | 10-second timeout accepts more SSE but creates unacceptable waiting. | p95 time to usable state; reject any multi-second queue tail. |
| P5 | A 60-second retry cooldown prevents reconnect storms. | SSE attempts per user and polling continuity. |
| P6 | Exponential retry cooldown reduces edge pressure better than fixed retry. | Edge 429/1200, retry attempts, recovery latency. |
| P7 | Conditional bracket polling with ETag/304 moves fallback cost below CPU saturation. | 200/304 ratio, payload bytes, API CPU and route p95. |
| P8 | Compact revision polling is cheaper than full bracket serialization. | Response bytes, serialization time and API CPU at equal users. |
| P9 | Active SSE users plus passive polling users are healthier than all-user SSE. | 10k mixed profile: usable-state p95, CPU, Redis, DB and edge errors. |
| P10 | At the accepted SSE ceiling, event fan-out rather than handshake load is the CPU test. | Hold active SSE stable, increase event rate until sustained API CPU is observed. |

### Selection rule

Reject a variant if users wait more than the configured fallback budget, if
unexpected 5xx/edge errors appear, or if conditional polling breaks state
freshness or permission behavior. Among the remaining variants choose, in
order: zero unexpected errors, p95 time to usable state below one second when
the fallback path is already available, event freshness, lowest API CPU, then
highest active SSE count. A high load-generator CPU sample is never a server
bottleneck result.

### Five follow-ups on the selected timeout/retry variant

| ID | Follow-up | Decision metric |
| --- | --- | --- |
| PF1 | Reduce retry cooldown by 2x. | Recovery speed without reconnect burst. |
| PF2 | Increase retry cooldown by 2x. | Edge pressure and user freshness trade-off. |
| PF3 | Add passive-tab polling backoff to 30–60 seconds. | CPU/HTTP reduction without stale visible state. |
| PF4 | Coalesce same-revision polling responses across one browser tab. | Duplicate requests and correctness under rapid events. |
| PF5 | Hold the selected active SSE ceiling and raise event fan-out gradually. | Sustained API CPU, event p95, Redis and PostgreSQL headroom. |

These ten plus five are a test matrix, not a claim that every variant has
passed. Public results remain authoritative; origin-local runs are diagnostic
only. The next promotion requires a public mixed 10k run with no long
handshakes, no unexpected edge errors and a separately measured CPU/fan-out
round.

### Local fallback A/B result

| Variant | Result | Meaning |
| --- | --- | --- |
| 1s handshake timeout | PASS | Stalled SSE moved to polling within the configured budget. |
| 3s handshake timeout | PASS | Same correctness and conditional-response behavior. |
| 5s handshake timeout | PASS | Same correctness and conditional-response behavior; rejected for the production UX budget. |

This local mock test deliberately does not choose the production winner: it
cannot measure healthy SSE handshake rate, Cloudflare edge behavior or VPS
CPU. The live A/B must compare active SSE count, time to usable state, polling
rate, edge errors and API CPU at each timeout.

## Current local candidate verification

- Web typecheck, lint and production build: passed.
- Browser regression with a stalled SSE handshake and polling fallback: passed.
- Backend SSE route, conditional-header, retained-load contract and focused
  QA tests: passed.
- Full local backend suite: not a clean gate because the local
  `platformdb_test` credential for `platform_test_user` is invalid; 860 tests
  ran and 153 failed during database setup. This is an environment failure,
  not evidence against the candidate.
- The bounded-admission candidate `c7874f3b` passed exact-SHA security/build
  `32958837837`, automatic deploy `32959335972` and production deploy/live
  smoke `32959345140`. The browser jitter/default-timeout candidate `e772bd76`
  passed exact-SHA security/build `32960850480`, automatic deploy `32961358905`
  and production deploy/live smoke `32961367264`; it is live on production.

## Bounded admission follow-up

The public 1,000-SSE staircase exposed a second queue after the
function-scoped DB lifetime fix. The stream DB semaphore had two slots but an
unbounded wait; the limiter Redis pool allowed a 20-second wait; and the
middleware's per-user lookup could wait on the ordinary API pool. The 5-second
public run reached server route p95 `19.2s` while API CPU was not sustained at
100%, so this was an admission queue, not proof of a CPU ceiling.

The next candidate bounds each admission wait to `0.5s`. Saturated initial
admission returns `503` with `Retry-After` and `Cache-Control: no-store`, so
the browser uses conditional revision polling. Saturated revalidation closes
the already-open stream safely because an HTTP status cannot be sent after
streaming has begun. Session and tournament permission checks remain
authoritative and fail closed; no Cloudflare, Nginx or application cap is
raised.

Local focused DB, stream authorization, limiter and load-harness tests pass,
including the 503 mapping. The public A/B gate for this candidate is response
time to either SSE 200 or deliberate 503, zero unexpected 5xx/1200, normal
polling fallback, and a decreasing server route p95. Only after that gate will
the staircase continue to 5k and 10k public virtual users.

The first public 5k attempt with the 1-second budget was not a product result:
4,972 opens timed out, while 28 cancelled HTTP contexts later emitted
`RemoteProtocolError` after roughly 47 seconds. The load harness now closes a
timed-out stream context with a 250ms bound and classifies a no-response
disconnect after the open budget as fallback-eligible. The regression test
protecting this measurement boundary passes.

The repaired public 5k run `32959670815` completed with exact cleanup
`32959892162`: 5,000/5,000 attempts became fallback-eligible in the 1-second
budget, with zero client errors, 429s, 503s or Cloudflare 1200 responses, but
zero streams reached HTTP 200. VPS API CPU averaged 68.7% (peak 141.7%),
PostgreSQL averaged 30.5%, Redis 0.3%, and no lock contention was observed.
This is a valid UX/fallback result, not a 5k SSE-capacity pass; Nginx logs
showed upstream SSE timeouts after the clients had already abandoned the
handshakes.

The public 1k low-burst A/B `32960050840` (`open_concurrency=32`) completed
with exact cleanup `32960232187`: 13 HTTP 200 streams, zero errors/429/503,
and 987 fallback-eligible attempts. Connect p50/p95/p99 was
`0.88/4.90/4.90s`; API CPU averaged 47.4% and PostgreSQL 12.4%, with no
locks. Lowering burst alone therefore did not produce a sub-second p95.

The current web candidate changes the default browser timeout to 1s, adds a
maximum 500ms randomized SSE open delay, and starts the first conditional
revision poll within a maximum 500ms after fallback. Local web lint, build,
typecheck and the focused stalled-SSE Playwright test pass. The next public
A/B must verify that this reduces edge-held abandoned streams before any
additional capacity claim.

## Public 10k fallback diagnostic and combined-run recovery

The deployed `e772bd76` public SSE-only run `32961754619` created 10,000 users
and 20 tournaments, then classified all 10,000 SSE attempts as fallback
eligible within the 1-second open budget. It produced zero HTTP 200 streams,
zero errors/429/503/1200 and zero active SSE connections. Server CPU averaged
58.0% for `deadlock-api` and 26.5% for PostgreSQL; sustained CPU saturation and
lock contention were false. This is a valid fast-fallback result, not proof of
10,000 persistent public SSE capacity. Exact cleanup `32962201314` removed the
fixture and preserved the control account.

The first combined public contour (`32962289708`, 1,000 SSE plus 10,000
polling tabs) was canceled after about 10 minutes without a summary. Abort
evidence showed the load-generator Python process at roughly 98% CPU while API
workers were only 2–3%; the test was measuring its own 512-connection queue and
180-second request timeout rather than the VPS. Exact abort `32964342080`
stopped only that process tree. Exact cleanup `32964417347` then recovered the
durable manifest and deleted 10,000 users and 20 tournaments, leaving zero
users, tournaments, sessions or audit rows and preserving
`aleksei.lisitsin1@gmail.com`.

The retained-load harness now bounds workload HTTP requests to 10 seconds,
closes SSE tasks and clients on cancellation, and bounds combined execution to
`open_stagger + polling_duration + request_timeout + 15s` (115s for the
previous parameters). A timeout is recorded as a diagnostic FAIL and still
emits the exact cleanup manifest; it cannot occupy the VPS for the 180-minute
supervisor limit. The focused SSE QA/unittest and shell syntax checks pass.

The deployed harness revision removes a separate measurement contaminant
exposed by the canceled combined run. SSE/combined fixture setup now creates only the
requested number of synthetic users and one public tournament on the single
Redis hot key; it does not run the browser profile's ready-check or assignment
workflow first. A 90-second fixture budget fails fast and leaves the durable
marker for exact cleanup. The full 20-tournament stateful fixture remains in
the browser-polling profile, where those workflow transitions are part of the
test rather than an SSE prerequisite.

The deployed diagnostic revision records Redis `PUBLISH` subscriber counts,
stream disconnects, keepalives and bytes in the compact summary. Public run
`32970619144` (32 SSE, open concurrency 16, one-second open budget) produced
7/32 HTTP 200 streams with connect p95 938ms, but 0/21 events and subscriber
counts `[0, 0, 0]`; API CPU averaged 36.3%, PostgreSQL 6.1%, and no lock or
connection saturation was observed. This was not a CPU result. The evidence
identified a shared-relay lifecycle race: `_unsubscribe_from_bracket_relay`
closed the Redis Pub/Sub client even while other queues remained subscribed.
The fix closes the shared resources only after the last queue leaves and adds a
regression test that closes one of two subscribers before delivering the next
event. Exact cleanup `32970738275` deleted 32 users and one tournament and
preserved `aleksei.lisitsin1@gmail.com`.

The post-fix public staircase on `8c0103e7` confirmed the relay result. The
32-SSE A/B delivered 24/24 events with Redis subscriber counts `[1, 1, 1]`,
event p95 about 40ms and connect p95 about 947ms. Public 1k admitted 6 streams
and delivered 18/18 events; public 5k admitted 4 and delivered 12/12; public
10k classified all 10,000 opens as fallback within one second with no
unexpected errors. API CPU remained below sustained saturation throughout.
Exact cleanups `32972274118`, `32972607636`, `32972913089` and `32973507881`
removed the fixtures and preserved the control account.

The first mixed 10k users + 32 SSE run `32973657012` exposed a load-harness
problem: 10,000 polling coroutines had no shared request-concurrency gate, and
the load generator reached 97.5% CPU while API CPU stayed near 1%. Its
bounded-run cancellation also waited on the overloaded gather. Abort
`32974261688` and exact cleanup `32974326125` recovered the process tree and
fixture. The harness now caps combined polling requests at the configured
concurrency and uses `asyncio.wait` with explicit task cancellation/draining so
the mixed test cannot turn a client-side queue into an unbounded VPS run.

The bounded mixed baseline rerun `32975890344` reached the VPS and stopped at
its 85-second execution budget with an explicit diagnostic FAIL. It created
10,000 polling tabs at request concurrency 20; partial workspace client
p50/p95/p99 was `374/711/1,149ms`. Server evidence showed `deadlock-api` at
104.1% average CPU and 152.8% peak, workspace origin p95 879.9ms/p99 2.09s,
PostgreSQL 17.3%, Redis 0.3%, and no lock/backend-wait contention. Exact
cleanup was `32976226545`.

The first single-tournament count-plan A/B (`99e465a3`, run `32978141716`,
cleanup `32978444085`) was rejected for this contour: API CPU stayed at
104.0%, workspace server p95 was 1.09s versus 0.88s baseline, and DB time
rose slightly. The candidate was reverted. The next H2 skips the published
assignment lookup only for `registration_open` detail workspaces. The domain
guard rejects assignment staging before `registration_closed`, so this saves
one read without weakening access checks, ETags, response shape or SSE
admission.

The H2 public run `32979781513` (exact cleanup `32980077832`) reduced the
workspace route from 6.0 to 5.0 SQL queries/request and average DB time from
246ms to 201ms. The full contour still measured workspace p95 909ms/p99
2.64s and API CPU 107.1%, so H2 is retained as a safe sub-optimization but is
not selected as the CPU winner by itself.

### CPU follow-up H3: response serialization boundary

The next candidate returns the already constructed Pydantic workspace payload
through `model_dump_json()` and a plain JSON `Response`, preserving body,
status, ETag and conditional `304` behavior while avoiding FastAPI's second
response-model validation/encoding pass. The live gate compares API CPU,
workspace p95/p99, payload bytes, conditional responses and error counts with
the same mixed contour.

The H3 public run `32981400437` reached the same bounded diagnostic budget and
was therefore a measured FAIL, not a hang. It recorded workspace p50/p95/p99
`383/996/2,070ms`, average DB time `198ms` at 5 SQL queries/request, API CPU
`106.6%` average (`144.6%` peak), PostgreSQL `17.1%`, Redis `0.27%`, and no
lock/backend-wait contention. This is not a clear improvement over H2, so H3
is not selected as the final winner. Exact cleanup `32981700615` succeeded.

### CPU follow-up H4: public registration-open workspace snapshot

H4 keeps the H2 registration-open query reduction and adds a small per-process
TTL snapshot only for the anonymous/public-shaped detail contract used by the
mass polling contour: `participants_limit=0`, offset `0` and
`include_current_user=false`. A cache hit still performs a cheap tournament
identity/status/visibility read; authenticated requests independently recheck
participant, invite and organizer/admin access before using the generic DTO.
Private/participant/manager responses never enter this cache. The snapshot is
bounded to 128 entries and 2 seconds, is invalidated after tournament status and
participant mutations, and remains an optimization of public representation
data rather than authoritative state. H4 must be rejected if the mixed contour
shows stale permission-sensitive fields, incorrect ETags/304s, unexpected
errors, or no measurable reduction in API CPU/route latency.

The first H4 dispatch `32986215002` is excluded from ranking: it used a `600s`
polling stagger while the H2/H3 comparison contour used the `85s` bounded
diagnostic budget, and its SSE subprofile returned three live-update `503`s.
Exact cleanup `32986648873` removed 10,000 users and one tournament and
verified zero synthetic users, tournaments, sessions and audit rows.

The corrected same-shape H4 run `32986759039` reached the same `85s` bounded
diagnostic budget. It executed 5,093 workspace requests before the combined
task timeout, with client p50/p95/p99 `297/586/773ms`; server workspace
p50/p95/p99 was `359/743/1,268ms`. Workspace SQL fell to `4.08` queries/request
and `163.8ms` DB time, compared with H3 `5.0` and `198.4ms`; API CPU fell to
`105.0%` average from `106.6%`, but remained sustained at the two-core ceiling.
There were no polling request errors, lock contention or backend waits. The
run is therefore a valid component A/B and the current H4 winner, but not a
full combined-capacity pass because the bounded workload did not complete.
Exact cleanup `32986913152` deleted 10,000 users and one tournament, preserved
the control account and verified zero remaining users, tournaments, sessions
and audit rows.

## Final unified SSE boundary — 2026-08-27

The deployed package ending at `2c551c50` retains signed HMAC tickets,
PostgreSQL-free ticketed opens, private fail-closed revalidation, shared
worker/tournament relay, SharedWorker deduplication, polling fallback and
global/source/user leases `3,000/32/4`. The relay formats each event once into
a bounded shared sequence buffer; this reduces fan-out overhead without
changing authorization, revocation or admission behavior.

The origin-local ticket profile passed at 15,000, 17,000 and 20,000 concurrent
60-second SSE streams with complete event delivery and zero errors. The
20,000 point (`33034469879`, cleanup `33034798652`) delivered `60,000/60,000`
events, with connect p95 `2.85s` and event p95 `6.52s`. API CPU was
`54.0%` average / `119.5%` peak; API cgroup memory reached about `985MB`, so
memory—not SSE admission, Redis or PostgreSQL—is the next origin boundary.

The public application cap remains deliberately `3,000`. With ticket opens,
the same target passed at 50/s, 75/s and 100/s with zero errors; connect p95
was `4.94s`, `15.83s` and `24.14s` respectively, while event p95 remained
about `1.42–1.66s`. A 5,000-attempt overflow respected the cap: 3,000
connected, 193 received expected 429 responses and the rest timed out in the
edge queue; no 503 or application error occurred.

The mixed production point (`33036740237`, cleanup `33037055264`) passed with
3,000 SSE and 10,000 polling users: 3,000 streams, 9,000 events and 10,000
polling requests completed with zero errors. API CPU averaged `80.9%` and
peaked at `140.3%`, with about `919MB` RSS; PostgreSQL and Redis showed no
lock/backend-wait or admission saturation. The remaining bottlenecks are
Cloudflare/transport opening queueing on the public path and API CPU/memory
on the origin path. Ten-thousand public persistent SSE and exact 180% CPU are
not claimed without operator-owned edge/VPS capacity changes and a new
protected measurement.
