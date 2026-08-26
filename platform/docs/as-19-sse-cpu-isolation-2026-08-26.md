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

The local candidate currently uses a 5-second browser handshake timeout and a
60-second SSE retry cooldown. The retained-load runner accepts the same
timeout as an explicit A/B parameter and records timeout/fallback eligibility
separately from unexpected errors. The browser value is a bounded build-time
configuration named `NEXT_PUBLIC_PLATFORM_SSE_OPEN_TIMEOUT_MS`; when unset it
defaults to 5 seconds.

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
| 5s handshake timeout | PASS | Same correctness and conditional-response behavior; retained as the current default pending live active-SSE comparison. |

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
- No commit, CI run or production deployment has been made for this candidate.

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
