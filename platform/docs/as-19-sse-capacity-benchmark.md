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
capacity. The reviewed capacity candidate raises the global ceilings to
application `10,240` global / `32/source`, Nginx `10,240/source + 10,240/global`, and
`worker_connections=32,768`, while retaining a per-user cap of `4` and
fail-closed Redis admission. The production load runner uses a signed,
secret-derived QA header to bypass only the application per-source bucket;
global, per-user, Redis fail-closed and Nginx limits remain active. Ordinary
clients never receive this bypass. These are capacity ceilings, not a claim
that a single VPS can sustain 10,000 streams; the claim is made only after the
staircase, combined run and resource evidence pass.

## Ordered protocol

1. Deploy the reviewed runner and limit candidate through the normal `dev`
   security/build and automatic production deployment chain. If a diagnostic
   identifies a measurement-blocking implementation issue, record it as a
   separate A/B hypothesis, deploy only that focused correction, and repeat
   the affected staircase point before ranking the ten variants. If the core
   matrix passes, run a separate reviewed overload extension above 10,000 to
   measure headroom and deliberate admission, without treating a deliberate
   `429` as a successful 10,000-user result.
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
logs, CPU/RAM/load, PostgreSQL connections and lock waits, and worker/backlog
signals where available.

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

## Ten hypotheses

These are executed against the same retained fixture and public route. The
first three establish the staircase; the remaining variants isolate opening
burst, hold time, fan-out and mixed polling effects.

| ID | Variant | SSE target | Hold | Open concurrency | Probe |
| --- | --- | ---: | ---: | ---: | --- |
| H1 | 1k staircase baseline | 1,000 | 60s | 256 | 3 events / 1s |
| H2 | 5k staircase baseline | 5,000 | 60s | 512 | 3 events / 1s |
| H3 | 10k gradual baseline | 10,000 | 60s | 512 | 3 events / 1s |
| H4 | 10k faster opening | 10,000 | 60s | 1,024 | 3 events / 1s |
| H5 | 10k bounded opening | 10,000 | 60s | 256 | 3 events / 1s |
| H6 | 10k long hold | 10,000 | 180s | 512 | 3 events / 1s |
| H7 | 10k fan-out burst | 10,000 | 60s | 512 | 20 events / 0.25s |
| H8 | 10k high fan-out opening | 10,000 | 60s | 1,024 | 20 events / 0.25s |
| H9 | 10k reconnect pressure | 10,000 | 60s | 512 | 1 reconnect, 3 events / 1s |
| H10 | 10k combined workload | 10,000 | 60s | 512 | 3 events / 1s + polling |

H10 uses `profile=combined`, a 30-second polling window and the established
300-second opening stagger. The other rows use `profile=sse`. H1/H2 are
diagnostic staircase points, not evidence that the server is limited to those
sizes.

## Five follow-up hypotheses

After selecting `Hn`, run five nearby variants rather than changing several
dimensions at once:

| ID | Follow-up change around `Hn` |
| --- | --- |
| F1 | halve the SSE opening concurrency |
| F2 | double the SSE opening concurrency, capped at 2,048 |
| F3 | double the hold duration |
| F4 | double probe event rate while keeping the opening shape |
| F5 | repeat the winner with one forced reconnect cycle |

The final winner is the highest-capacity variant with zero unexpected errors
and measurable headroom. If two variants tie, prefer the one with lower p95,
lower CPU and fewer Redis/DB waits. No value is accepted only because it
allows the client generator to create more tasks.

## Operator commands

Run from the reviewed `dev` branch in a low-traffic window:

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
`sse_event_interval` and `sse_reconnect_cycles` per the hypothesis table. For
the combined run use `profile=combined`, `sse_users_per_tournament=500`, and
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
tournament graph and control-account preservation before deletion.

## Results

Results are added after the CI/deploy gate and each production run. Detailed
JSON and bounded logs remain in the GitHub artifacts/VPS run root; this document
stores only the measured conclusion and run identifiers.

- Limit candidate CI/deploy: `32819954714` / production `32820833942` passed for
  `da1435c`. Signed QA-bypass and failed-run summary fix: exact CI
  `32826238833`, auto-deploy `32826794523`, production deploy/live smoke
  `32826801617`, all passed for `39499dfc`.
- Ten-hypothesis matrix: pending.
- Follow-up matrix: pending.
- Combined 10,000-user SSE + polling gate: pending.
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
