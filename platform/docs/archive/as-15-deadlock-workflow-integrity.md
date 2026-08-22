# AS-15 Deadlock workflow persistence and concurrency integrity — closed

- Status: Archived / resolved
- Closed: 2026-08-22
- Implementation commit: `ad8afb2f2da2ea5d2032f4ad11654a39fbafb3d7`
- Merged production commit: `87525bab34c473ac51708eba1e242b7baa6a1462`
- Production deployment run: `32574455599`
- Verified production release: `gha-32574455599-1-87525bab34c4-20260822T125945Z`
- Alembic head: `20260822_0040`

## Original finding

Ready-check, captain, assignment generation, roster publish/lock and profile
dream-slot writers did not all share one durable database serialization model.
Competing requests could create ambiguous workflow rows, write a late vote,
lock a roster after a terminal tournament transition, or merge replace-all
slot payloads. Redis job locking was advisory and could not protect a route or
another worker from a conflicting database write.

## Remediation delivered

Every durable Deadlock workflow writer now locks the stable `Tournament` row
with `SELECT ... FOR UPDATE`, reloads authoritative lifecycle state and then
performs its secondary write. Replace-all dream slots lock the owning `User`
row before delete/reinsert. Ready votes revalidate active round, active
participant and profile eligibility under the tournament lock.

Migration `20260822_0040` adds check constraints and partial/canonical unique
indexes for workflow values and cardinality. It normalizes the retired stored
`private` visibility alias to `invite_only`, fails clearly on incompatible
historical data and removes an invalid interrupted concurrent index before a
retry. Historical migration `20260714_0031` receives the same preflight/retry
protection for normalized public tournament names.

## Verification and deployment

Focused independent-session coverage proves competing ready starts, vote versus
close, captain retries, stale terminal-state roster generation/lock, invite
claim lifecycle and concurrent dream-slot replacement. The complete backend
suite and production web build passed for merge commit `87525bab`.

The automatic production release installed exact merge commit `87525bab` as
`gha-32574455599-1-87525bab34c4-20260822T125945Z`. Release preflight confirmed
a restore-verified database backup, head `20260822_0040`, healthy services and
adequate disk. Origin and public deployment smoke passed, including API live/
ready, web, static assets, CSP and security endpoint checks.

## Retained invariant

PostgreSQL is the final concurrency authority for durable workflow state.
Redis may coalesce automation work but must never replace the parent-row lock,
same-transaction lifecycle revalidation or database cardinality guard.
