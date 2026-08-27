# Ready Check and bracket realtime boundary

- Status: Accepted
- Owner: Platform maintainers
- Date: 2026-08-27

## Decision

The public tournament catalog is request-driven. `/tournaments` does not open
realtime connections or run a background poller: initial navigation, filter
changes and pagination issue ordinary list requests, and a user who wants a
fresh catalog reloads or navigates again.

Ready Check is the only product-critical realtime flow. It uses one global
application pool across all tournaments, while the browser consumes that pool
only from the visible matching tournament-detail route. The provider may stay
mounted in the root layout for state continuity, but unrelated pages never
create a critical Ready Check stream. The current production guard is
`READY_CHECK_SSE_GLOBAL_LIMIT=3000`; `READY_CHECK_SSE_HARD_TARGET=10000` is a
capacity-test target, not a production setting. Source and authenticated-user
limits remain independent safety guards. Ready Check uses one stream per
authenticated user to prevent multi-tab duplication; the compatibility bracket
contour retains its separate four-per-user limit.

The authenticated `/ready-check/agenda` read is the only PostgreSQL-backed
admission planning request. It returns a session-bound HMAC proof for the
user’s state probes and one proof containing the bounded set of eligible
tournament IDs for the global SSE stream. Proofs expire at the applicable
Ready Check end with a bounded maximum; the response exposes the stream-proof
expiry so a multi-check agenda can refresh before that horizon. The proof is
not an authorization grant for voting; PostgreSQL remains authoritative for
workflow actions. The browser refreshes this agenda at authentication/app
start, after registration or leave mutations, when visibility is restored and
before a bounded stream proof expires; pathname transitions do not
independently trigger the PostgreSQL read.

## Admission policy

The agenda computes one cohort for Ready Checks in the same preparation
horizon. Preparation starts from expected demand, current connected demand,
simultaneous checks and the measured safe opening rate (currently 25 opens/sec),
with bounded safety and preparation windows. A deterministic user slot spreads
scheduled opens through that window. Simultaneous tournaments receive
proportional planning quotas, with deterministic remainder rounding; Redis’s
atomic global lease is the final admission guard.

There is no server-side admission queue. A user arriving after preparation has
started but before `T` is a late, high-priority attempt and can use any
available capacity. Users outside the scheduled quota use the cheap polling
fallback at `T`; after `T`, the initial tournament HTTP state is authoritative
and the agenda does not create a late SSE stream merely to discover that state.

`GET /ready-check/state` verifies the HMAC proof locally and performs one small
Redis lookup. It returns only `revision` and `status` (`waiting`, `active` or
`closed`). It does not load workspace, participants, teams, bracket data or
perform a PostgreSQL join/session lookup on every poll.

The browser does zero fallback requests before `T` when a scheduled stream is
healthy. At `T` it performs an immediate state probe only for polling-admitted,
rejected, failed, timed-out or disconnected streams, then polls at
approximately 1.5 seconds while visible. Hidden tabs stop polling and close
the stream; returning to visible immediately probes again. Stream opening has
a bounded handshake timeout and reconnect uses full jitter over the measured
recovery window. A stream is created only for the visible matching tournament
detail, and navigation away releases it immediately. `READY_CHECK_STARTED`
closes the critical stream after its authoritative probe; a distant future
check does not keep the stream open or reserve a slot. Its own
`admission_open_at` can schedule a later stream when that tournament page is
visible. The Ready Check end plus a short client timeout remains the safety
boundary.

The Redis relay retains a small bounded sequence window. If a subscriber falls
behind and an event is evicted, the relay emits an explicit `resync` frame
instead of silently skipping the sequence. The browser treats both `resync`
and `READY_CHECK_STARTED` as wake-up hints and probes the user-scoped state;
only that authoritative result can change the button. A stream established
around `T` also probes when its connected frame arrives, covering the race in
which the start event was published before the stream subscribed.

## Bracket policy

The browser no longer opens bracket SSE. A bracket page uses a short-lived
viewer-bound HMAC probe ticket and `GET /tournaments/{slug}/bracket/probe`,
which performs one Redis lookup and returns only the current revision/status.
The full bracket endpoint is requested only initially or after the probe
reports a higher revision. This keeps bracket polling separate from Ready Check
critical capacity and avoids repeatedly serializing the full workspace.

The old bracket SSE route may remain during the compatibility window, but it is
not part of the current web client or capacity plan and must not be counted as
the Ready Check pool.

## Capacity gate

The hard target is tested through the public Cloudflare path in stages:

```text
3,000 -> 5,000 -> 7,500 -> 10,000 critical Ready Check SSE
```

Each stage must use the new profile: short Ready Check streams, event-triggered
release, overflow polling only at `T`, and the tiny Redis-backed state probe.
The run records edge status/errors, connect rate, active leases, Redis and
PostgreSQL connections, API CPU/RSS, request latency, state-event delivery and
polling volume. It stops at the first resource or safety boundary. A successful
origin-only run or an old full-workspace polling run does not authorize raising
the public cap.

The retained-load workflow selects this path with `profile=sse`,
`sse_scope=ready-check`, and `sse_admission_mode=ticket`. The 3,000 stage may
use the production default; the 5,000/7,500/10,000 stages pass the requested
stage through a short-lived signed QA capacity override. That override is
accepted on the public path only for this ticketed Ready Check contour, and
never changes the default production cap or permits high-cap bracket testing.
The proportional allocator is covered for simultaneous unequal demand, spare
capacity, late arrivals and existing occupancy; those tests validate the
planning contract, not production fairness until the public staircase is run.

Every `/ready-check/agenda` request is included in request-performance
telemetry. The retained-load report exposes p50/p95/p99 setup duration, SQL
query count/time and PostgreSQL pool wait for this route; global connection and
wait snapshots remain the load-run resource evidence. It also records relay
gap/resync count, attempted versus established streams and release latency.
No shared planning cache is introduced before those measurements demonstrate a
bottleneck.
