# AS-19 — SSE capacity and combined-load benchmark

- Status: Active measurement plan and controlled limit experiment
- Owner: Platform maintainers
- Started: 2026-08-25

## Objective

Measure the deployed public bracket SSE behavior separately from the existing
10,000-user browser-polling gate, then measure both paths together. The test
must distinguish a deliberate admission response (`429`) from an application
failure (`5xx`, unexpected disconnect or client error) and must leave no
synthetic data behind.

The previous deployed contour was application global `128`, Nginx
`8/source + 160/global`, and Nginx `worker_connections=768`. That contour
rejected the 1,000- and 5,000-attempt probes before they tested application
capacity. The reviewed capacity candidate initially raised the global ceilings
to application `10,240` global / `32/source`, Nginx `10,240/source + 10,240/global`,
and `worker_connections=32,768`, while retaining a per-user cap of `4` and
fail-closed Redis admission. Public runs then showed Cloudflare Error 1200 at
about 3,500 concurrent streams while the VPS still had no sustained CPU,
PostgreSQL or lock saturation. The selected next hypothesis lowers only the
application global ceiling to `3,000`, leaving Nginx at `10,240`, so excess
viewers receive a fast application `429` and can use polling rather than wait
in the Cloudflare edge queue. Current public retained-load runs send no
source-bucket bypass header: application global/source, per-user, Redis
fail-closed and Nginx limits remain active. These are capacity ceilings, not a claim
that a single VPS can sustain 10,000 persistent streams; 10,000 virtual users
must be measured as a mixed active-SSE/passive-polling workload.

## Current public combined-load checkpoint

Baseline `3d09aa0a`; public runs `1787697719` (1k), `1787698223` (5k), and
`1787698626` (10k, pool40) passed with exact cleanup. Pool512 run `1787699600`
(cleanup `1787699601`) removed generator queueing but showed CPU ~91% and peak
pool wait ~1.9s. The selected contour is the frontend-aligned lean workspace
contract (`include_current_user=false`) with client pool512; F1 `1787700510`
(stagger 450s) and F2 `1787700610` (600s) passed. F2 was best: p95/p99
262/388ms, CPU ~49.6%, zero errors, 1,201 conditional 304s, 32/32 SSE events.
The post-release exact-SHA gate `1787701010` also passed: p95/p99 277/420ms, 1,200 304s, 32/32 SSE events, zero errors; these runs open only 32 SSE, not 10,000 persistent SSE.
Local DB integration remains blocked by the developer `platformdb_test` password and is a CI item.

Workspace follow-up results, including the final post-deploy gate:

| Run | Variant | Result | Key evidence |
| --- | --- | --- | --- |
| `1787700510` | lean, pool512, stagger450s | PASS | p95/p99 279/435ms; CPU 59.4%; 0 errors |
| `1787700610` | lean, pool512, stagger600s | BEST | p95/p99 262/388ms; CPU 49.6%; 0 errors |
| `1787700710` | lean, pool256, stagger300s | FAIL | 9 workspace 500s from pool16 timeout |
| `1787700810` | viewer `bracket_summary` | PASS, not best | p95/p99 365/647ms; CPU 86.4% |
| `1787700910` | server early workspace `304` | FAIL | 20 errors; 18 workspace 500s from pool16 timeout |
| `1787701010` | deployed final lean gate, pool512, stagger600s | PASS | p95/p99 277/420ms; 1,200 304s; 0 errors |

Every run used the public domain without a bypass and was cleaned immediately;
each removed 10,000 users and 20 tournaments, left zero fixture rows, preserved
the control account. The early-304 live patch was restored byte-for-byte to
baseline and health returned 200; it is rejected because first reads still need
the pool and extra preflight work did not prevent timeouts.

## Ordered protocol

1. Run the public Cloudflare path as the acceptance contour. A single
   origin-local run is allowed only as a diagnostic control; it must never be
   used to claim customer capacity or replace a public result. If a candidate
   changes runtime behavior, promote it through the normal `dev`
   security/build and automatic production deployment chain before measuring
   it. A deliberate application `429` is acceptable only when it is returned
   quickly and the client falls back to polling; Cloudflare `1200`, origin
   `5xx`, unexpected disconnect or client error is a failure.
2. Run the ten hypotheses below one at a time. Clean each run before starting
   the next one; a failed or canceled run is still cleaned.
3. Select the best hypothesis by zero unexpected errors/503s first, then
   admission success, event delivery, p95/p99 latency, CPU/RAM/FD headroom,
   PostgreSQL pool/lock wait and Redis backlog.
4. Run the five follow-up hypotheses around the selected winner.
5. Select the final profile using the same ordering and record the evidence.
6. Run the combined profile with 10,000 polling users, 10,000 virtual tabs and
   the configured SSE target. Polling uses the selected active/passive profile;
   SSE uses the same retained fixture and a bounded event probe.
7. Clean every retained run with its own exact cleanup workflow run ID. A
   canceled or failed measurement is still cleaned and is never reported as a
   successful capacity result.
8. Record the staircase and combined conclusions here and in `CURRENT.md`.

## Metrics and interpretation

The runner records connection attempts, connected streams, `429`/`503`/other
responses, connection latency, keepalives, reconnects, bytes, event delivery
latency, polling status/304 counters, HTTP p50/p95/p99, request-performance
logs, CPU/RAM/load, Nginx/API/PostgreSQL/Redis TCP connections, PostgreSQL
connections and lock waits, and worker/backlog signals where available. Redis
TCP connections are sampled without issuing extra Redis commands; this was
important for the per-stream baseline and remains useful for detecting relay
or limiter regressions.

The Redis messages are explicitly transport/fan-out probes with type
`qa_sse_probe`; they do not mutate authoritative tournament state. A successful
probe demonstrates delivery through the bracket event channel, not correctness
of a tournament workflow writer. Workflow mutation load remains owned by the
existing write-burst and browser-polling profiles.

Expected admission behavior for the new target is no unexpected rejection up
to the selected capacity ceiling. Any unexpected `5xx`, `503`, client error,
lease-release failure or sustained resource saturation is a failed measurement
requiring diagnosis. A `429` is only acceptable when the tested variant is
deliberately above a configured ceiling; it is not a successful 10,000-user
result.

## Research conclusions and local-first gate

The current production evidence does not support raising admission limits as
the next change. The reliable contour is still below the configured ceilings,
and the failed 1,000-stream runs show edge `500` responses and incomplete
chunked bodies rather than deliberate `429` admission. The next measurements
must first classify where a stream ends and whether Redis fan-out scales with
one connection per stream.

The design constraints are based on the following primary documentation and
published engineering reports:

- [FastAPI advanced dependencies](https://fastapi.tiangolo.com/advanced/advanced-dependencies/)
  documents `Depends(..., scope="function")`, which closes a yielded database
  resource before a `StreamingResponse` starts. The SSE route uses this
  isolated dependency graph while periodic authorization opens short-lived
  sessions; security revalidation is not removed.
- [NGINX proxy module documentation](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
  defines `proxy_read_timeout` as an idle interval between upstream reads, not
  a total response lifetime. The 660-second experiment was therefore rejected
  after it worsened the measured result; keepalives and stream lifecycle must
  be tested independently from the timeout.
- [Cloudflare Error 524 guidance](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/)
  and [Cloudflare Error 520 guidance](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-520/)
  require correlation of edge identifiers, origin response status and response
  timing before classifying an edge `500` as an application or proxy failure.
- [Gunicorn's design guidance](https://docs.gunicorn.org/en/stable/design.html)
  recommends asynchronous workers for streaming and long polling. Worker count
  is a process-concurrency choice, not a one-worker-per-client setting.
- [SQLAlchemy pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html)
  requires an explicit pool/overflow/timeout budget. Increasing the pool to
  hide a stream-held connection is not an accepted experiment.
- [Redis Pub/Sub](https://redis.io/docs/latest/develop/pubsub/) is at-most-once
  transport. Shared subscribers or relay-like fan-out therefore require an
  explicit reconnect, ordering and missed-event/snapshot policy before they
  can replace the current per-stream subscription.
- [Slack's realtime architecture](https://slack.engineering/real-time-messaging/)
  separates channel routing from gateway-held connections, while
  [Discord's million-user report](https://discord.com/blog/maxjourney-pushing-discords-limits-with-a-million-plus-online-users-in-a-single-server)
  describes passive connections, relays and bounded fan-out. These are
  architectural hypotheses for local testing, not a reason to copy a relay
  into the authoritative workflow path without measurements.

The local-first execution gate is:

1. **L0 — application origin.** Use the isolated `test` environment,
   `platformdb_test` and Redis DB 15. Run 1,000, 5,000 and 10,000 synthetic
   stream attempts against the loopback API with short holds (5/15/60s), then
   repeat only the best two contours. Capture HTTP status, complete-body
   errors, event delivery, Redis TCP connections and PostgreSQL connections.
2. **L1 — local Nginx.** Repeat the winning L0 shapes through a disposable
   local Nginx configuration with the production 60-second idle timeout and
   15-second keepalives. Compare origin and proxy `upstream_status`,
   `request_completion`, `connection_requests` and request IDs.
3. **L2 — fan-out variants.** Compare the baseline one-Pub/Sub-client-per-stream
   implementation with the narrowly scoped shared-subscriber/relay candidate for
   one tournament. The prototype must preserve authorization before every
   private event, explicit revision/snapshot recovery and disconnect cleanup.
4. **L3 — selected candidate only.** Promote one local winner to the normal
   exact-SHA CI/deploy chain, then run one production diagnostic contour before
   spending time on the full 10+5 matrix. A local pass is not production
   authorization, but a local failure is sufficient to reject a candidate.

The local gate is strict: no unexpected `5xx`, malformed/incomplete stream
body or client error; `429` is counted as deliberate admission only when the
test is intentionally above a configured cap. A valid candidate must also
show that active SSE count does not pin PostgreSQL connections for the stream's
whole lifetime. The local load generator itself must report its `RLIMIT_NOFILE`
and have enough descriptors, otherwise the result is a generator failure rather
than a server-capacity result.

## Engineering hypothesis register

These are the ten technical hypotheses behind the workload variants below.
They are deliberately separated from the 1k/5k/10k staircase so a failed
opening shape is not mistaken for a failed architectural change.

| ID | Hypothesis | Current evidence/status |
| --- | --- | --- |
| E1 | A request-scoped DB session was held for the whole SSE stream. | Accepted and fixed with the dedicated SSE router and function-scoped dependency. |
| E2 | Duplicate stream authorization/snapshot queries amplify setup CPU and DB time. | Accepted; participant snapshot/query reuse materially reduced stream DB work. |
| E3 | Revalidating idle streams every keepalive is unnecessary work. | Accepted; 30-second checkpoints removed sustained API CPU saturation in the local 1,000-stream contour while retaining event and idle revalidation. |
| E4 | Opening backpressure is more valuable than raising worker count. | Accepted; bounded `open_concurrency=64` reached the local 10,000-stream target, while faster/unbounded shapes were slower or failed at the boundary. |
| E5 | The load generator's file-descriptor ceiling distorted the 1,000-stream result. | Confirmed confounder and fixed; subsequent runs use/report `32768` descriptors. |
| E6 | The 60-second Nginx read timeout caused the incomplete streams. | Rejected by the isolated 660-second experiment; the longer timeout regressed CPU/load and errors. |
| E7 | Cloudflare/origin closes are being misclassified because edge and Nginx completion fields are missing. | Accepted as an observability requirement; the loopback probe isolates origin behavior and the runner now retains request/edge/Nginx correlation fields. |
| E8 | One Redis Pub/Sub connection per stream becomes the next hard limit. | Accepted and rejected as the production shape: the local per-stream baseline reached 9,982/10,000 but produced 18 limiter `503`s and Redis peaked at 10,000 clients. |
| E9 | One subscriber/relay per tournament can reduce fan-out without moving authoritative state out of PostgreSQL. | Accepted for the local transport candidate: one relay per API worker/tournament reached 10,000/10,000 with 10,000/10,000 events and Redis peak 146. Private authorization remains in the stream path. |
| E10 | Compact revision/delta events reduce CPU and bytes enough to move the boundary. | Partially accepted: the event contract is already a compact revision/match delta trigger; full delta-response savings still need a payload benchmark. |

E1–E6 retain the earlier production evidence. E7–E9 now have isolated local
evidence; E10 is partially implemented but its full delta-response savings
remain unmeasured because the 10k transport winner was selected without
changing the authoritative bracket payload. The local result is not a
production authorization: the exact-SHA CI/deploy gate and one guarded live
diagnostic remain required.

## Ten hypotheses

The following ten A/B hypotheses were tested locally against the isolated
origin. They compare one causal change at a time and include the earlier
production evidence where that was the only safe place to test the edge or
proxy contour.

| ID | Hypothesis / comparison | Result |
| --- | --- | --- |
| H1 | Stream-held DB session vs function-scoped session | Function scope removed the 1:1 PostgreSQL lease; the original 1k failure moved from pool exhaustion to edge/origin pressure. |
| H2 | Duplicate stream authorization/snapshot query vs reused access context | Reduced SQL work, but was not sufficient alone for the 1k strict gate. |
| H3 | Revalidation every keepalive vs 30-second checkpoint | The 30-second checkpoint retained private-event checks and removed sustained API CPU saturation in the 1k contour. |
| H4 | Load-generator `RLIMIT_NOFILE=1024` vs `32768` | `1024` produced `Errno 24` and was invalid; `32768` made the 1k test a server measurement. |
| H5 | Nginx `proxy_read_timeout=60s` vs `660s` | `660s` regressed CPU/load/errors; `60s` retained. |
| H6 | `open_concurrency=16/32/64` | Open32 was the strict 512 contour; open64 was the best local 1k/5k/10k opening shape. |
| H7 | One Redis Pub/Sub client per stream vs worker/tournament relay | Per-stream failed at 10k with 18 limiter `503`s and Redis peak 10,000; relay passed 10k with Redis peak 146. |
| H8 | Per-request/non-blocking limiter pool vs shared blocking pool `64/20s` | Pool32 caused limiter-unavailable `503`s; blocking64/20s produced zero `503`s at 10k. |
| H9 | Ramp-only stream test vs barrier-held persistent streams | The barrier makes all attempts reach terminal status before the hold and event probe; it prevents early clients disappearing before the target is open. |
| H10 | 10-second event hold vs 30-second hold with 5-second settle | `167/10,000` events at the short hold was a measurement-tail failure; the selected hold delivered `10,000/10,000`. |

H1–H10 are local engineering A/Bs, not a production capacity claim. The
combined 10,000-user polling+SSE profile remains a separate release gate.

## Five follow-up hypotheses

After selecting `Hn`, run five nearby variants rather than changing several
dimensions at once:

| ID | Follow-up around the relay winner | Result |
| --- | --- | --- |
| F1 | Open 1k streams with `open_concurrency=32` | Pass: 1,000/1,000 HTTP 200 and events, p95 7.8s, Redis peak 147, load-1m peak 0.66. |
| F2 | Open 1k streams with `open_concurrency=128` | Pass: 1,000/1,000 HTTP 200 and events, p95 6.8s, Redis peak 149, load-1m peak 0.94; not promoted to 10k without a 10k confirmation. |
| F3 | Hold 1k streams for 20s instead of 10s | Pass: 1,000/1,000 HTTP 200 and events, p95 7.3s, Redis peak 149, load-1m peak 0.99. |
| F4 | Five events at 0.2s instead of one event | Pass: 5,000/5,000 events, p95 7.4s, Redis peak 150, load-1m peak 0.85. |
| F5 | Increase post-open settle from 5s to 10s | Pass: 1,000/1,000 HTTP 200 and events, p95 6.9s, Redis peak 149, load-1m peak 1.22. |

The final 10k origin transport winner is relay + coalesced authorization +
blocking limiter pool `64/20s` + bounded `open_concurrency=64` +
barrier/settle/hold profile. It has zero unexpected origin errors, full event
delivery and PostgreSQL headroom, but is not a public capacity claim: the live
Cloudflare contour fails around 3,500 concurrent streams. Public acceptance
now requires fast application admission plus active-SSE/passive-polling
fallback; origin-local success cannot promote a candidate by itself. See the [`CPU-isolation matrix`](as-19-sse-cpu-isolation-2026-08-26.md); public contour is authoritative and a load-generator or probe Redis failure is invalid.
## Operator commands

Run from the reviewed `dev` branch in a low-traffic window. Production public
mode is the acceptance path; `origin-local` is allowed only once as a control:

```bash
gh workflow run platform-production-retained-load-matrix.yml \
  --repo StrayForest/old_sparky --ref dev \
  -f confirmation=RUN-PRODUCTION-RETAINED-LOAD-MATRIX \
  -f control_email=aleksei.lisitsin1@gmail.com \
  -f concurrency=80 -f profile=sse \
  -f sse_connections=1000 -f sse_users_per_tournament=50
gh run watch <load-run-id> --repo StrayForest/old_sparky --exit-status
```

Use `sse_connections=5000` with `sse_users_per_tournament=250`, then
`sse_connections=10000` with `sse_users_per_tournament=500`. Set
`sse_open_concurrency`, `sse_duration`, `sse_event_count`,
`sse_event_interval`, `sse_reconnect_cycles` and `sse_open_timeout` per the
hypothesis table. For the combined run use `profile=combined`,
`sse_users_per_tournament=500`, and
the reviewed polling/SSE durations. Always run:

```bash
gh workflow run platform-production-retained-load-cleanup.yml \
  --repo StrayForest/old_sparky --ref dev \
  -f confirmation=DELETE-PRODUCTION-RETAINED-LOAD \
  -f load_run_id=<load-run-id> \
  -f control_email=aleksei.lisitsin1@gmail.com
gh run watch <cleanup-run-id> --repo StrayForest/old_sparky --exit-status
```

If the load workflow is canceled while the SSH step is active, use the exact
abort workflow first. The cleanup/recovery path accepts `sse` and `combined`
manifests and still validates marker, report path, synthetic ownership,
tournament graph and control-account preservation before deletion. When the
Actions API is unavailable, run the same fixed production cleanup supervisor
on the VPS with the exact SHA, load run ID and control email; never issue a
broad database delete.

## Results

Results are added after each measured run. Detailed JSON and bounded logs remain
in the VPS run root; the pre-release workspace A/Bs above were intentionally
run directly on the public server before committing or starting CI. The next
release gate is to keep only the selected, measured changes, update tests/docs,
commit to `dev`, then wait for exact-SHA security/build, auto-deploy and live
smoke. A combined 10,000-user SSE + polling gate remains required after that
release candidate is verified.
- Local origin probe: `platform/tools/platform_sse_origin_probe.py` uses only
  `PLATFORM_ENVIRONMENT=test`, `platformdb_test`, Redis DB 15 and exact cleanup.
- Local staircase evidence uses a barrier so all attempts reach terminal status:

  | Profile | Result | Connect p95 | Redis peak | PostgreSQL peak |
  | --- | --- | ---: | ---: | ---: |
  | 1k, per-stream Pub/Sub | 1,000/1,000 HTTP 200; 3,000/3,000 events | 14.8s | 1,017 | 31 |
  | 5k, per-stream Pub/Sub | 5,000/5,000 HTTP 200; 15,000/15,000 events | 126.2s | 5,017 | 31 |
  | 10k, per-stream Pub/Sub | 9,982/10,000 HTTP 200; 18 limiter `503`s | 402.3s | 10,000 | 31 |
  | 1k, worker/tournament relay | 1,000/1,000 HTTP 200; 3,000/3,000 events | 10.9s | 83 | 31 |
  | 5k, worker/tournament relay | 5,000/5,000 HTTP 200; 15,000/15,000 events | 99.3s | 4,602 | 31 |
  | 10k, relay + blocking limiter pool | 10,000/10,000 HTTP 200; 10,000/10,000 events | 309.4s | 146 | 31 |

  The final 10k local run held streams 30s after a 5s settle, took 425.2s,
  had zero probe errors, API peak 10,000, load-1m peak 1.72, Redis rejected
  delta 0 and PostgreSQL fixed at 31 connections. The 10s hold variant
  delivered only 167 events and was rejected before the settle/hold repeat.
- Diagnostic H1 attempt `32823477661` did not reach a valid SSE result: the
  setup completed, but the application source bucket held at 32 (`connected=171`,
  `max_active=32`, `rejected_other=829`, `errors=149`, no events). It was
  cleaned by `32823765006`: 1,000 users and 20 tournaments deleted, zero fixture
  users/tournaments/sessions/audit rows remained, and the control account was
  preserved. This run is excluded from hypothesis ranking.
- Valid H1 baseline after signed QA source-bucket bypass: load
  `32825319293`, cleanup `32825654336`. It reached the application rather than
  Nginx admission, but returned `200=136`, `500=864`, `max_active=30`, no
  events; PostgreSQL peaked at 196% CPU and the bracket-events route had p95
  about 6.2s. It is retained as a failed baseline, not a capacity result.
- A/B diagnostic `39499dfc` removed the duplicate middleware session lookup for
  signed QA streams. Load `32827142929`, cleanup `32827388111`: `200=80`,
  `500=920`, `max_active=35`, no `429/503`, no events. The result did not
  improve the bottleneck; it isolated that the long-lived response still held
  request-scoped DB connections.
- Focused A/B `b742b136` explicitly closed the endpoint DB session before
  `StreamingResponse`. Load `32828905208`, cleanup `32829138910`: `200=694`,
  `500=306`, `max_active=396`, `events=25`, no `429/503`; `/csrf` p95 was
  `971ms` and PostgreSQL peaked at `94.6%` CPU. This materially improved the
  result but did not remove all failures, showing that router/auth dependencies
  still retained request-scoped sessions.
- The first implementation of the next focused A/B applied
  `scope="function"` globally to shared authentication and tournament
  dependencies. Its CI run `32829249835` was canceled after the backend job
  exceeded the historical runtime by roughly 5x; local `pg_stat_activity`
  showed an ordinary invite-claim request holding an `idle in transaction`
  session while another request waited on a transactionid lock. That variant
  is rejected. The corrected implementation keeps ordinary API dependencies
  request-scoped and puts the SSE route in a dedicated router with a
  function-scoped auth/policy/serialization graph. Stream revalidation still
  uses short-lived sessions. Its first production H1 run `32832475533` was
  cleaned by `32832797705`: `200=620`, `500=380`, `429=0`,
  `max_active=422`, event count 38, connect p95 21.49s. PostgreSQL peaked at
  195.0% CPU, API at 134.7%, bracket SSE server p95 was 57.9s, and no
  PostgreSQL connection-peak or lock-wait flag was raised. This is a failed
  capacity point, but it confirms that the original one-connection-per-SSE
  signature is no longer the only bottleneck. The runner now records bounded
  status-body diagnostics for non-200 responses; the result is not ranked
  until the diagnostic rerun and staircase continue.
- Intermediate contour on the same deployment: `256` SSE passed in load run
  `32834441834` and was cleaned by `32834698357` (`200=256/256`, `errors=0`,
  `max_active=256`, events 87, connect p95 5.19s). The next `512` step failed
  in load run `32834773835` and is pending cleanup result: `200=415/512`,
  `500=97`, `errors=77`, `max_active=348`, connect p95 10.94s; all sampled
  500s were Cloudflare plain `Internal Server Error` responses. It was cleaned
  by `32835036424`. The current reliable contour is therefore 256, with the
  failure boundary between 256 and 512 under this opening shape.
- A backpressure A/B at `512` connections with opening concurrency `128`
  (`32835148133`, cleaned by `32835407140`) improved the result to
  `200=468/512`, `500=44`, `errors=70`, `max_active=398`, events 89 and
  connect p95 11.66s, but still failed the no-unexpected-errors criterion.
  Lower opening concurrency helps, but does not make 512 reliable.
- Query-reuse A/B on the deployed dedicated stream router reused the full
  tournament snapshot loaded by the stream authorization dependency for the
  endpoint visibility check. It preserved the same authorization and
  periodic revalidation semantics and removed one duplicate admission query.
  Load `32837162933`, after CI/deploy `32836308720` / `32836811526` /
  `32836818533`, reached `200=486/512`, `500=26`, `errors=41`,
  `max_active=445`, events 91 and connect p95 10.83s; no `429` or `503` was
  observed, and sampled failures were Cloudflare `500 Internal Server Error`
  responses. Exact cleanup `32837600679` verified 1,000 fixture users and 20
  tournaments deleted, zero remaining fixture users/tournaments/sessions/audit
  rows, and preservation of `aleksei.lisitsin1@gmail.com`. This is the best
  measured 512/open128 contour so far, but it still fails the zero-unexpected-
  errors criterion; the reliable contour remains 256.
- H2 staircase (`5,000` SSE, `open_concurrency=512`, 60s hold) failed much
  earlier than the nominal admission ceilings: load `32837747171` reached
  `200=1,856/5,000`, `500=3,143`, `errors=308`, `max_active=338`, zero
  delivered events and connect p95 221.6s. No `429` or `503` was returned;
  sampled failures were Cloudflare `500 Internal Server Error` responses.
  Exact cleanup `32838201646` deleted 5,000 fixture users and 20 tournaments
  and verified zero remaining fixture users/tournaments/sessions/audit rows
  while preserving the control account. This confirms that raising admission
  limits alone cannot produce the 5k step; opening pressure must be reduced or
  the origin request path must be made cheaper before higher targets are useful.
- A 512-connection opening-backpressure contour with `open_concurrency=64`
  (`32838425845`, exact cleanup `32838635589`) reached all `512/512` HTTP
  200 responses, `max_active=504`, 234 events and connect p95 11.72s, with no
  429/503 or non-200 response. It still recorded 8 client-side stream errors,
  so it is near the boundary but not a strict pass. This is currently the
  strongest 512 contour and supports opening backpressure as a useful
  hypothesis; before testing open32, the reliable zero-error contour remained
  256.
- Reducing the opening burst to `open_concurrency=32` produced the first
  strict pass at 512: load `32838825035`, exact cleanup `32839036740`,
  `200=512/512`, `errors=0`, `max_active=512`, 284 events, no `429/503` and
  connect p95 12.30s. Resource sampling showed no sustained CPU, PostgreSQL
  connection or lock saturation; the remaining classification was the same
  SSE DB-time hotspot. This is the current reliable 512 contour.
- A 1,000-connection staircase at the same `open_concurrency=32` in load
  `32839100405` (cleanup `32839300689`) reached `200=990/1,000`,
  `errors=17`, `max_active=988` and 315 events, but failed the strict gate.
  It showed sustained and peak `deadlock-api` CPU saturation without
  PostgreSQL connection/lock saturation. The current strict staircase point
  is therefore 512.
- The duplicate pre-subscription authorization A/B deployed in `bd02dbe9`
  retained the same access checks and removed one query before Redis
  subscription. At 512/open64, load `32840244186` with exact cleanup
  `32840480141` was a strict pass: `200=512/512`, zero errors, 393 events and
  connect p95 10.86s. It still showed CPU peak/load-average flags but no
  sustained API CPU, PostgreSQL connection or lock saturation. At
  1000/open32, load `32840531009` with cleanup `32840726728` reached
  `990/1,000`, `errors=16`, `max_active=985` and 398 events, with sustained
  API CPU; it therefore failed the strict gate.
- The idle-revalidation A/B deployed in `79b023f0` keeps mandatory access
  validation before every private event but checks idle streams every 30s
  instead of every keepalive. At 1000/open32, load `32841823646` with exact
  cleanup `32842030758` reached `989/1,000`, `errors=12`, `max_active=988`
  and 469 events. Sustained CPU and load-average flags disappeared; the
  remaining classified hotspot was database time, but the zero-error gate
  still failed. A 1000/open16 contour (`32842094082`, cleanup
  `32842288384`) reached `989/1,000` with 13 errors, 423 events and p95
  20.06s; sustained API CPU returned, so open16 is worse than open32.
- The next focused A/B removes the endpoint's duplicate participant lookup.
  The stream access dependency has already loaded the tournament and
  participant status into its access context; the endpoint can derive the
  visibility flag from that context and avoid a second read. This does not
  change session validity, role checks, private-event revalidation or the
  fallback path when the context is unavailable. It was deployed in
  `7105dde8` and measured at 1000/open32 in load `32843777739`, with exact
  cleanup `32844061492`: `989/1,000` HTTP 200 streams, `errors=11`,
  `max_active=989`, 452 events and connect p95 18.68s. Crucially, every
  sampled error was client-side `ConnectError: [Errno 24] Too many open
  files` or `All connection attempts failed`; there were no HTTP error
  responses and no 429/503 admission response. The run therefore does not
  measure an origin failure and cannot establish 1000 capacity.
- The runner confounder was fixed in `d5cd6f93`: the SSE child process raises
  its soft `RLIMIT_NOFILE` to 32768 when the hard limit permits it and logs the
  effective soft/hard values. This changes only the load generator process,
  not Nginx, PostgreSQL or the API service. The valid repeat at 1000/open32,
  load `32845174078` with exact cleanup `32845451618`, confirmed
  `nofile soft=32768 hard=32768` and reached `998/1,000` HTTP 200 streams,
  `2` Cloudflare 500 responses, `5` incomplete-chunk client errors,
  `max_active=994`, 410 events and connect p95 20.14s. No 429/503, sustained
  CPU/load-average, PostgreSQL connection or lock saturation was observed.
  The classified hotspot remained database time. Stream admission work fell
  to 5.26 average SQL queries and 161.7ms average DB time; this supports the
  participant-snapshot optimization, but the strict 1000 gate still fails.
  Compact JSON artifacts now retain the effective load-generator `nofile`
  limits and bounded connection/response error samples for the next repeat.
- Repeat the same 1000/open32 shape once more before changing opening
  concurrency or application semantics. The repeat determines whether the
  two edge 500s are stable origin pressure or near-boundary variance.
- The same-shape repeat on the runner-fixed deployment was load
  `32846652953`, with exact cleanup `32846866673`. It confirmed the failure is
  reproducible at the boundary: `995/1,000` HTTP 200 streams, `5` Cloudflare
  500 responses, `4` incomplete-chunk client errors, `max_active=995`, 401
  events and connect p95 18.64s. The runner reported
  `nofile soft=32768 hard=32768`; there were still no 429/503 responses,
  sustained CPU/load-average saturation, PostgreSQL connection-peak or lock
  contention flags. The remaining classification is a DB-time hotspot during
  mixed setup work plus a small origin/edge failure rate at this opening
  shape, so 1000/open32 is not a strict pass. Cleanup verified deletion of
  1,000 synthetic users and 20 tournaments, zero remaining fixture users,
  tournaments, sessions or audit rows, and preservation of
  `aleksei.lisitsin1@gmail.com`.
- Because the same-shape result failed in the same way twice, the next
  experiment may change one variable: compare a gentler `open_concurrency=16`
  and a faster `open_concurrency=64` at 1000, while retaining the fixed runner
  and exact cleanup. No Nginx/Redis admission ceiling will be raised blindly;
  both runs must show whether the current limit is opening pressure or a
  deeper stream/origin boundary.
- The opening-shape comparison on the same deployment produced: open16 load
  `32847009440` / cleanup `32847218607` with `999/1,000` HTTP 200, one
  Cloudflare 500 and eight incomplete-chunk errors; open64 load `32847283128`
  / cleanup `32847524729` with `1,000/1,000` HTTP 200, zero HTTP errors and
  eight incomplete-chunk errors. Both retained `nofile=32768/32768`, had no
  429/503 or PostgreSQL connection/lock saturation, and remained classified
  as DB-time hotspots. Open64 is the best handshake result, but neither is a
  strict pass because the stream body did not complete cleanly for eight
  clients.
- The next candidate targets a concrete boundary found in the deployed
  configuration: the SSE Nginx location used `proxy_read_timeout=60s`, equal
  to the benchmark hold. The candidate raises only that route timeout to
  `660s` (above the supported 600s stream lifetime); application leases,
  per-user/global admission, keepalives and authorization are unchanged.
  This is a timeout-lifecycle experiment, not an admission-limit increase.
- The `660s` timeout candidate was deployed as `c3e5752f` and tested at
  open64 in load `32848671574`, with exact cleanup `32848969483`. It regressed
  to `999/1,000` HTTP 200, one Cloudflare 500 and 13 incomplete-chunk errors;
  the run also raised the load-average flag and PostgreSQL CPU hotspot. The
  candidate is rejected and the SSE timeout is restored to `60s`; this
  experiment does not justify increasing proxy lifetime or admission limits.
- The relay winner was then packaged as `4f4b5863` and passed the exact-SHA
  security/build gate `32865611322`, automatic deploy `32866234695` and
  production deploy/live smoke `32866244193`. The local 10k strict result is
  therefore deployed, but that release gate does not itself prove 10k public
  SSE capacity.
- The first guarded live 10k SSE diagnostic on that release was load
  `32866749952`, cleanup `32866955426`. It did not reach SSE: after creating
  10,000 synthetic users, the first authenticated `GET /auth/csrf` used for
  tournament setup returned a Cloudflare `504` after about 30.2s. No SSE
  connection, event or admission result is inferred from this run. Production
  sampling showed PostgreSQL peak CPU but no sustained API CPU, Redis rejection
  or PostgreSQL connection/lock flag; the setup path is therefore a separate
  measured bottleneck, not evidence that the relay failed.
- After compacting progress checkpoints and redeploying as `3ed9d4a3`, the
  repeat live run `32869459781` reached `10,000/10,000` HTTP 200 streams with
  zero 429/503/other responses and zero client errors. Its connect p50/p95/p99
  were `113.8s/202.0s/210.4s`; API CPU was the dominant resource signal
  (average `73.9%`, peak `136.2%`), while no PostgreSQL lock contention was
  observed. However, the pre-barrier runner published too early and recorded
  only `9` events, so this is an opening-capacity pass, not an event-fan-out
  pass. Cleanup is still required before the next strict rerun.
- The live failure exposed write amplification in the fixture runner: user
  creation persisted a growing JSON identity list to `PreprodTestRun` after
  every 500-user batch, immediately before API setup. The runner now stores a
  bounded progress sample during that phase and keeps the complete identity
  inventory for the final report; interrupted-run recovery reconstructs the
  exact marker-scoped user set from synthetic email identities. This keeps
  cleanup fail-closed while removing setup-report serialization from the SSE
  measurement. The runner is now being tightened further with an all-attempts
  barrier and an explicit `events >= connected × event_count` gate; that change
  must pass local verification and a new exact-SHA deploy before another live
  10k fan-out attempt.
- Exact cleanup of the failed live run deleted 10,000 synthetic users and
  verified zero remaining fixture users, tournaments, sessions or audit rows;
  `aleksei.lisitsin1@gmail.com` was preserved. The combined polling+SSE live
  contour, reconnects, slow-consumer behavior and delta-response savings are
  still open gates.
- The opening-capacity repeat was cleaned by `32870304826`: 10,000 synthetic
  users and 20 tournaments were deleted, with zero remaining users,
  tournaments, sessions or audit rows and the control account preserved. The
  next strict run must use the barrier/event-delivery runner before any claim
  is made about complete 10k fan-out.
- The strict barrier/event-delivery runner was deployed as
  `da203bd1f78dd52658b9a05f3964218266de094d` after security/build
  `32871147286`, automatic deploy `32871730205` and production deploy/live
  smoke `32871738322`. Its guarded run `32872200332` again stopped before SSE:
  after 10,000 users were inserted, the first authenticated `GET /auth/csrf`
  returned Cloudflare `504` after about 30.2s. The runner's strict fan-out gate
  was therefore never reached and no SSE capacity conclusion is inferred.
  Exact cleanup `32872451869` deleted 10,000 users and verified zero remaining
  fixture users, tournaments, sessions or audit rows while preserving the
  control account. The next setup A/B refreshes PostgreSQL statistics for
  `users`, `sessions` and `user_roles` after direct fixture inserts and records
  bounded active-query samples from `pg_stat_activity`; this is a setup
  diagnostic, not a change to API/DB/SSE admission limits.
- The retained-load supervisor now observes the VPS directly during the remote
  command: bounded `ps`, socket, Redis client/rejection and PostgreSQL activity
  snapshots are written every five seconds, followed by bounded API/worker
  `journalctl` and Nginx access/error tails. The read-only evidence is exported
  as `server-observability.log` beside the compact matrix summary, so a future
  setup stall can be classified while it is occurring instead of inferred only
  from the final CI status.
- Two observer-enabled setup attempts were aborted after exact cleanup because
  no valid matrix summary was produced. The supervisor now retains bounded VPS
  snapshots, raw QA traceback and partial reports; neither attempt is a
  capacity measurement.
- The next observer-enabled 1,000-SSE run (`32881488410`) reached the
  application cleanly: 1,000/1,000 connections returned HTTP 200, with zero
  errors, 429s or 503s, and all 1,000 expected events delivered. Connect
  latency was p50/p95/p99 8.72/17.77/18.00 seconds; event delivery latency was
  395.5/574.2/584.7 ms. The overall workflow was nevertheless red because
  the performance collector crashed while summarizing PostgreSQL active-query
  samples. The collector now keeps that row list separate from the system
  sample window and has a regression test. Exact cleanup `32882110537`
  removed the fixture and preserved the control account. Treat this as a
  valid 1k SSE transport result, but rerun the repaired collector before using
  it as the staircase gate.
- The first 5,000-SSE public-origin run (`32883773066`) reached the VPS only
  partially: 3,535 connections returned HTTP 200 and 1,465 returned Cloudflare
  503 Error 1200 (`cache_connection_limit`, with `Retry-After: 60`). The
  application emitted zero 429s; PostgreSQL lock contention was not observed
  and sustained CPU saturation was false. This is an edge-capacity result,
  not an origin-capacity result. Exact cleanup `32884651890` removed the
  10k-user/20-tournament fixtures and preserved the control account. The
  retained-load harness now has an explicit `origin-local` SSE mode using
  `127.0.0.1:8010` with the canonical production `Origin` header, so the
  remaining 5k/10k staircase can separate Cloudflare edge capacity from VPS
  origin capacity.
- The one permitted origin-local control run (`32886113934`) accepted all
  5,000 connections and delivered all 5,000 events with zero errors, 429s or
  503s. Its connect latency was p50/p95/p99 29.4/89.3/95.7 seconds. This is
  not a customer-facing success: the direct-origin result shows that the VPS
  eventually holds the streams, while the long admission time gives
  Cloudflare enough queue pressure to emit Error 1200 on the public path.
  The control run must be cleaned with the exact manifest; the cleanup
  validator accepts its loopback origin only for `mode=sse` when
  `request_origin` is still the canonical public origin. Public-origin runs
  remain the acceptance gate.
- Public ramp A/B `open_concurrency=16` (`32888021777`) still failed through Cloudflare: 3,466/5,000 HTTP 200, 1,533 Error 1200, one 502 and zero app 429s; slower opening did not solve the edge queue. The current bounded-admission candidate `c7874f3b` instead makes overload fail fast and keeps Cloudflare/Nginx limits unchanged.
- On the repaired public 5k run `32959670815` (exact cleanup `32959892162`), all 5,000 attempts became fallback-eligible within 1s with zero client errors/429/503/1200, but zero HTTP 200 streams; API CPU averaged 68.7% and no locks were observed. This is a valid UX fallback result, not an SSE-capacity pass.
- The 1k low-burst A/B `32960050840` (exact cleanup `32960232187`) reached 13 HTTP 200 streams and 987 fallbacks; connect p95 was 4.90s. Candidate `e772bd76` is now deployed with a 1s browser timeout, up to 500ms SSE jitter and prompt conditional polling; public 10k run `32961754619` (cleanup `32962201314`) produced 10,000 fast fallbacks and no persistent SSE 200s.

## Final unified SSE package — 2026-08-27

The historical staircase above is superseded for the current production
decision by the protected package ending at `578771b3`. It combines signed
short-lived SSE admission tickets, PostgreSQL-free ticketed opens, the
existing fail-closed Redis global/source/user leases (`3,000/32/4`), a shared
worker/tournament relay, one SSE per tournament through SharedWorker where
available, and polling fallback. Private streams retain periodic session and
participant revalidation. The Redis limiter pool is bounded at `512` with a
finite `2s` wait; this is a resource-capacity fix, not a protection bypass.

The exact release chain for the final code was security/build
`33009663151`, auto-deploy `33010232007`, and production deploy/live smoke
`33010239695`, all successful for the same SHA.

| Final contour | Result | Key evidence |
| --- | --- | --- |
| Ticket vs legacy public control, 32 opens | Ticket `32/32` HTTP 200 and `96/96` events; legacy `28/32` HTTP 200 with four controlled `503`s | Ticket path avoids the legacy PostgreSQL admission bottleneck; protection remains active |
| Public ticket, 1,000 opens, 5s timeout | `0/1,000` persistent SSE, 1,000 fast fallbacks, zero errors/429/503 | Customer-facing edge/transport queue dominates this wave |
| Origin-local ticket, 1,000 opens, 30s timeout | `1,000/1,000` SSE and `3,000/3,000` events, zero errors/429/503 | Connect p95 `7.30s`; API peak `68.8%`; no DB waits/locks |
| Public 10k browsing + 32 SSE | `10,000/10,000` workspace requests; `32/32` SSE; `96/96` events; zero errors/429/503/timeouts | SSE p95 `1.13s`; workspace p95 `324ms`; API peak `121%`; PostgreSQL peak `80%`, no waits/locks |
| Origin-local ticket, 3,000 opens, 30s timeout | `2,650/3,000` SSE; `7,950/7,950` events; 350 open timeouts; zero errors/429/503 | API peak `76%`; Redis peak `4%`; no single remaining origin bottleneck |

All valid retained runs were followed by exact cleanup, with zero synthetic
users, tournaments, sessions and audit rows remaining and the control account
preserved. A requested 3,000-open, 60-second run was rejected by QA input
validation before fixture setup because the supported timeout maximum is 30
seconds; it contributes no capacity result.

The final stopping verdict is: safe origin handshake improvements are
complete. The ticket path removes PostgreSQL from normal opening, the Redis
limiter pool no longer generates resource-starvation `503`s, and the combined
10k-user product contour passes. The public 1k five-second wave still falls
back because of edge/transport handshake queueing, so this work does not claim
10k persistent public SSE or the full 180% two-core target. Further progress
requires edge/transport architecture or a separately authorized infrastructure
capacity change; raising application caps would weaken the current protection
without evidence of a new safe limit.
