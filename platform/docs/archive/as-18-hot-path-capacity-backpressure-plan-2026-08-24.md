# AS-18 — Hot-path capacity and backpressure plan

- Status: Implementation and release verification in progress
- Owner: Platform maintainers
- Date: 2026-08-24

## Iterative load-measurement protocol

The remaining performance work follows a hypothesis-driven staircase. Each
step uses the same fixture shape, duration, polling cadence and observability;
only the virtual-user count changes:

| Step | Virtual users | Gate |
| --- | ---: | --- |
| baseline | 1,000 | durable report and no unexplained queue growth |
| scale | 5,000 | same pass criteria; compare p50/p95/p99 and saturation curves |
| final | 10,000 | same pass criteria plus stable finalization and exact cleanup |

Before changing code, capture client latency/bytes/status ratios, request and
SQL timing, pool checkout wait, PostgreSQL connections/waits/locks, CPU/RAM/load,
Nginx connections, Redis/Celery backlog/retries and event-loop progress. Add
temporary high-volume diagnostics only behind bounded QA flags and keep reports
privacy-safe. A canceled, timed-out or recovered run is evidence of a harness
or capacity failure, never a passing load result.

The development loop may run uncommitted source in an isolated local or
pre-production runtime so that one bottleneck can be measured quickly. It must
not mutate `sparkydb` or the protected production account. Production code is
released only from a reviewed commit through the exact-SHA security/build,
automatic deployment and live-smoke chain; an uncommitted direct production
deploy is not an accepted release path.

For every iteration, record the hypothesis, changed limit or code path,
before/after metrics, remaining ceiling and exact cleanup result in this plan
before moving to the next step. Do not raise pool, worker, SSE or HTTP limits
merely to make a run pass; first prove that the previous limit is the active
bottleneck and that CPU, database and downstream capacity remain available.

## A/B experiment matrix

The staircase is evaluated through at least ten controlled experiments. An
experiment changes one main variable at a time and keeps the fixture, route
mix, auth model, report schema and cleanup policy constant. Existing runs are
evidence for the matrix only when their configuration is recorded exactly;
they are not retroactively treated as passing results.

| ID | Hypothesis family | Main variable | Decision signal |
| --- | --- | --- | --- |
| A01 | burst arrival | open stagger 10s vs 60s | client queue and server pool wait |
| A02 | HTTP ceiling | 80 vs 200 connections | errors and DB pool timeout |
| A03 | API parallelism | one vs two API workers | p95, errors, CPU |
| A04 | DB pool shape | 3+1 vs 5+0 per API worker | pool wait without budget breach |
| A05 | sustained arrival | 5k at 60s vs 180s stagger | backlog growth and p95 |
| A06 | active/passive fan-out | all active vs hidden-tab mix | request rate, bytes, CPU |
| A07 | client ceiling | 40 vs 80 HTTP connections | queue depth vs server saturation |
| A08 | polling cadence | default revision delay vs bounded cadence | requests/user and freshness |
| A09 | payload path | workspace detail vs compact summary mix | response bytes and serialization |
| A10 | final scale | 10k with the selected safe profile | stable completion and cleanup |

After A01–A10, select the winner by a documented score: zero transport/5xx
errors first, then bounded cleanup, then p95/p99, then CPU and pool wait, and
only then throughput/freshness. Generate five follow-up hypotheses around the
winner (one change per run) and repeat the same score. A run that only looks
better because it skips requests, shortens the profile, or hides an error is
not a winner; those effects must be reported as backpressure or coverage.

## Executed experiment evidence

All runs below used the isolated `platformdb_test`/Redis runtime, exact-marker
cleanup and bounded reports. A short run means a 30-second polling window; it
does not claim sustained 10-second polling for the entire 300-second arrival
ramp. Errors include HTTP 5xx and transport failures.

| Run | Main change | Result | p95 / p99 | CPU / errors |
| --- | --- | --- | ---: | ---: |
| A01 | 1k, 2 API workers, stagger 10s → 60s | fail → fail | 15,471ms / 22,622ms → 1,140ms / 2,436ms | 1 → 2 |
| A02 | 1k HTTP pool 80 → 200 | fail → fail | 15,471ms / 22,622ms → 20,357ms / 27,435ms | 1 → 14 |
| A03 | 1 API worker → 2 | fail → fail | 18,631ms / 28,487ms → 18,211ms / 26,217ms | 4 → 2 |
| A04 | API pool 3+1 → 5+0 | fail → fail | 18,211ms / 26,217ms → 15,471ms / 22,622ms | 2 → 1 |
| A05 | 5k stagger 60s → 180s | fail → pass | 95,144ms / 112,327ms → 291ms / 421ms | 1 → 0 |
| A06 | 1k active → 70/30 visible/hidden | fail → pass | 1,140ms / 2,436ms → 357ms / 494ms | 2 → 0 |
| A07 | 1k HTTP pool 80 → 40 | fail → fail | 1,140ms / 2,436ms → 912ms / 2,700ms | 2 → 6 |
| A08 | 1k 60s window → 30s diagnostic window | fail → pass | 1,140ms / 2,436ms → 333ms / 452ms | 2 → 0; shorter coverage |
| A09 | 5k full workspace/bracket mix with conditional reads | pass | 272ms / 406ms | 0 |
| A10 | 10k active, pool 5+0, stagger 300s | fail | 22,923ms / 51,368ms | 949 |

The first five follow-up hypotheses were then run around the 5k
pool-5/stagger-180 winner:

| Follow-up | Variant | Result | p95 / p99 | CPU / errors |
| --- | --- | --- | ---: | ---: |
| F01 | stagger 240s | pass | 288ms / 451ms | 52/53%, 0 |
| F02 | stagger 150s | pass | 303ms / 444ms | 68/68%, 0 |
| F03 | 30/70 hidden/visible mix, stagger 120s | fail | 11,481ms / 22,016ms | 75/76%, 2 |
| F04 | API pool 4+0 | pass | 300ms / 462ms | 59/58%, 0 |
| F05 | HTTP pool 40 | pass; best 5k tail | 272ms / 406ms | 57/57%, 0 |

F05 won the 5k latency comparison, but the 10k confirmation exposed the
remaining pool ceiling. Additional capacity checks measured 10k passive with
API pool 5+0/8+0/12+0 as 633/106/0 errors respectively. HTTP20 with pool5
produced 309 errors, so reducing the client pool alone was not a solution.
The final short-window confirmation with pool12+0, HTTP40 and stagger300
passed both passive and active-only 10k profiles:

| Final profile | Result | p50 / p95 / p99 | CPU | PG max | cleanup |
| --- | --- | ---: | ---: | ---: | --- |
| 10k passive, 30s window | pass | 118 / 349 / 584ms | 67/67% | 35 | exact |
| 10k active-only, 30s window | pass | 119 / 315 / 496ms | 70/71% | 35 | exact |

The 300-second active-only sustained diagnostic remains a documented
boundary, not a hidden failure: it produced 51 transport errors, p95 about
600 seconds and only 19,626 of 300,000 fixed expected GETs while the local
load generator consumed about 84% of both cores. Ten thousand tabs with a
300-second opening ramp and a bounded 30-second polling window is therefore
the accepted no-VPS-increase target. Continuous 10-second polling for all
10,000 tabs is beyond this two-core contour and is not claimed as supported.

The selected release settings are API pool `12+0` per worker, worker pool
`2+0` with concurrency two, total connection budget `32`, load-generator HTTP
pool `40`, active-tab opening stagger `300s`, and a `30s` polling window. The
production wrapper passes these bounded values explicitly; its generic
`concurrency` input is retained for the ordinary matrix, while the browser
profile uses measured setup concurrency `20` and no longer expands the browser
HTTP pool.

The final local staircase on that selected runtime was repeated after the
tooling/docs changes:

| Users | Result | p50 / p95 / p99 | CPU | executed / 304 | cleanup |
| ---: | --- | ---: | ---: | ---: | --- |
| 1,000 | pass | 90 / 469 / 645ms | 59/59% | 2,187 / 541 | exact |
| 5,000 | pass | 97 / 325 / 508ms | 66/66% | 6,801 / 1,164 | exact |
| 10,000 | pass | 119 / 315 / 496ms | 70/71% | 12,570 / 1,936 | exact |

## Objective

Reduce hot-row serialization, fan-out, payload and background-work pressure on
the current two-CPU VPS without moving authoritative workflow state out of
PostgreSQL or increasing the server size. The target is a measured workload of
10,000 persisted virtual users with bounded active polling; it is not a promise
of 10,000 persistent SSE connections.

## Intended behavior

- Join capacity is represented by durable participant-capacity slots. A request
  claims one free slot with `FOR UPDATE SKIP LOCKED`, then creates the unique
  `(tournament_id, user_id)` participant row in one short transaction. The
  tournament row is reserved for lifecycle transitions and reconciliation, not
  ordinary slot selection.
- Ready votes continue to use the existing 32 counter shards and the unique
  `(round_id, user_id)` vote key. Ordinary votes do not lock the tournament row;
  round close/start and participant exclusion remain lifecycle transactions.
  The deferred database guard rejects a vote recorded after close while
  preserving a vote timestamped before the close commit.
- Polling responses expose a stable revision token. Unchanged conditional reads
  return `304 Not Modified`; changed reads use compact summaries and explicit
  `delta`/expanded views. Full rosters remain limited to active bracket and
  authorized management flows.
- Background tasks use existing Redis/Celery with explicit priority queues,
  bounded worker concurrency, retry/backoff and observable backlog. API and
  worker database pools have explicit limits and one documented connection
  budget; unlimited overflow is not allowed.
- Mutations remain idempotent where retries can repeat a side effect. Redis
  may coalesce work, but PostgreSQL remains the correctness authority.

The reviewed branch implements these boundaries in migrations `20260824_0042`
and `20260824_0043`, the participant-capacity service, tournament routes,
SQLAlchemy engine configuration, Celery worker configuration and the retained
browser-polling harness. The release has not yet passed the exact-SHA GitHub
gate or production deploy chain.

## Writers and lock order

| Workflow | Durable writers | Lock order |
| --- | --- | --- |
| Join/withdraw/restore | registration route, participant-management route, automation cleanup | join: free capacity slot → participant unique insert; lifecycle/capacity mutation: tournament → participant; restore: tournament → slot |
| Ready vote | vote route, automation close/exclusion, worker transition | ordinary vote: round/participant eligibility under conditional mutation; lifecycle: tournament → ready round → votes/shards |
| Bracket/roster | organizer route, automation, assignment worker | tournament → captain/assignment/roster rows → candidate users in ascending user id |
| Profile slots | profile route and account replacement flow | owning user/profile → slots |

The final database uniqueness/check constraints remain the guard against
concurrent writers. Any migration must be expand-first and safe to retry on a
populated database.

## Data and schema impact

The participant-capacity representation is the only new durable state proposed
for the join path. It must be reconciled against active participant rows and
must not grant access by itself. Ordinary capacities use a bounded free-slot
inventory; capacities above the 1024-row inventory allocate sparse slot rows on
demand. Existing participant status and invite/capacity serialization rules
remain authoritative. No database partitioning, PgBouncer, Redis-authoritative
workflow state or global permission cache is introduced.

## Verification and load criteria

- Focused independent-session tests cover slot claims, duplicate join retries,
  full-capacity races, vote-versus-close/exclusion, conditional revision reads,
  queue priority/backoff and pool configuration validation.
- The retained load matrix reports client phase p50/p95/p99, response bytes,
  route SQL timing, pool checkout wait, lock wait, CPU/RAM, PostgreSQL pressure,
  Celery backlog/retries and fan-out counts.
- Run the exact-SHA GitHub security/build workflow before merging/pushing to
  `dev`. A successful current-head run must feed the automatic production
  deploy chain; a stale or missing status blocks deployment.
- Load tests run only through the retained pre-production/prod operator
  workflows, with marked fixture cleanup. They do not target `sparkydb` and do
  not run against an unapproved production database.

## Destructive data reset gate

Before the reset, identify the target environment, take a restore-verified
backup when the target is persistent, and resolve exactly one protected user by
email/UUID. Delete the rest through foreign-key cascades, remove the protected
user from every tournament while retaining the user/profile/roles/access rows,
then verify zero tournaments, participant rows, workflow rows, sessions and
audit rows remain except the explicitly retained account/access graph. If the
protected identity or target database is ambiguous, stop and request the
operator identifier; do not infer it from a synthetic test account.

## Rollback risk

Application changes can roll back through the normal immutable release path.
Schema additions are expand-only and remain compatible with the previous
release. A destructive database reset cannot be rolled back by application
rollback; it requires the restore-verified backup and explicit operator
decision. Load-test fixtures must be cleaned by exact run marker, never by a
broad user/table delete.

## Execution checklist

1. **Completed:** resolve the protected account, create the restore-verified
   backup and perform the verified production reset.
2. **Completed in branch:** implement join capacity slots and idempotent
   duplicate behavior.
3. **Completed in branch:** preserve sharded ready votes and remove unnecessary aggregate/parent-row
   contention from the ordinary vote path.
4. **Completed in branch:** add conditional revision/ETag response behavior and
   retain active/passive polling.
5. **Completed in branch:** set explicit API/Celery pool and queue/backpressure
   budgets.
6. **Completed in branch:** add metrics, focused tests, CI/pre-deploy test
   ownership, the 20×500 browser-polling profile and exact retained-load
   cleanup support; remove stale code/docs.
7. **Completed in branch:** make the 10k browser profile measure polling rather
   than fixture creation: use at most 32 persisted participants per tournament,
   run state setup behind bounded gates, pass tournament slugs explicitly, and
   fail auto-assignment setup after five minutes instead of waiting for the
   retained-load ceiling. Bound the load-generator client pool to 40
   connections, open tabs over 300 seconds and use a 30-second active polling
   window while retaining 10,000 virtual tabs. Join/ready-vote contention
   remains covered by the write-burst profile.
8. Run documentation and focused checks, then the GitHub security/build gate.
9. Commit, push/merge to `dev`, observe auto-deploy and production smoke.
10. Run the approved retained load matrix, clean exact fixtures and archive
    compact before/after evidence.

Operational finding from the first live browser-polling attempt (2026-08-24):
the browser harness reached a long async-assignment wait and the GitHub
runner cancellation stopped its SSH client without stopping the detached
remote supervisor. The shared lock then correctly blocked three cleanup
attempts. The retained-load contour now has a server-side 180-minute timeout,
an exact run-ID abort workflow, and a fail-closed recovery path that rebuilds
only the missing browser report from the matching durable `PreprodTestRun`
row. This recovery is for deletion, not for turning an interrupted run into a
passing performance result.

The cleanup log also exposed a second-order async resource-lifecycle issue:
the exact cleanup entrypoint disposed SQLAlchemy's async engine from a second
event loop after a successful delete. The cleanup entrypoint now owns one
event loop for both deletion and disposal, so successful cleanup no longer
emits cross-loop asyncpg errors.
