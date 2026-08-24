# AS-18 — Hot-path capacity and backpressure plan

- Status: Implementation and release verification in progress
- Owner: Platform maintainers
- Date: 2026-08-24

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
   retained-load ceiling. Join/ready-vote contention remains covered by the
   write-burst profile.
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
