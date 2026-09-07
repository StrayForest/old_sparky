# Active authenticated HTML/TTFB candidate — 2026-09-07

Status: candidate ready for reviewed production A/B. Owner: Platform
maintainers.

This work order continues the open authenticated first-byte target from the
archived [attribution report](../docs/archive/performance-authenticated-html-ttfb-2026-09-07.md).
It owns one bounded runtime candidate and does not reopen the completed
13-profile performance matrix.

## Objective and baseline

Reduce `authenticated-page-load-v1` HTML TTFB p95 from the retained baseline
of `2568.859 ms` to `<1000 ms`, while preserving the read path, Ready Vote,
authentication/security semantics, no-JavaScript header behavior, backend
budget and exact fixture cleanup.

The fresh exact-profile attribution found API CPU pressure near 100% per core
and `/auth/bootstrap` pool checkout p95 `951.800 ms` with zero authenticated
admission wait. Production uses two API workers, a DB pool of `24` per worker,
and authenticated-read admission `32` per worker with no waiters. PostgreSQL
query execution is sub-millisecond on average for both bootstrap queries, so
query merging is not this candidate.

## Candidate

Profile `authenticated-read-admission-24x8` keeps the process-local
authenticated-read admission enabled, changes the in-flight limit from `32`
to `24` to match each worker's DB pool, and permits at most `8` bounded
connection-free waiters with a `250 ms` timeout. The total
admitted-plus-waiting envelope remains `32`; API workers, DB pool size/overflow,
pool pre-ping, connection budget, Ready Vote admission and retry policy remain
unchanged.

The hypothesis is that moving the burst queue out of PostgreSQL pool checkout
reduces CPU scheduling and pool contention without increasing accepted
concurrency. This is a runtime-only, reversible change; rollback is the
existing `authenticated-read-admission-32` profile.

## A/B protocol

1. Deploy the reviewed candidate SHA through the normal `dev` exact-SHA chain.
2. Run `authenticated-page-load-v1` with the unchanged 20,000-user,
   40-tournament fixture and exact cleanup.
3. Compare against the retained baseline and the fresh diagnostic window:
   HTML TTFB/page p95 and p99, Nginx upstream header, `/auth/bootstrap` total,
   admission wait/shed, pool checkout/hold, SQL time/count, API CPU,
   PostgreSQL backends/locks, HTTP correctness, Ready Vote and cleanup.
4. Restore `authenticated-read-admission-32` if the candidate regresses read
   latency, produces any unexpected status/timeout/520/522, sheds outside its
   bounded contract, exceeds backend `52`, breaks Ready Vote/security, or
   fails exact cleanup.

The candidate is accepted only if it improves authenticated HTML TTFB p95 in a
same-contract run and passes all safety gates. Otherwise it is rejected and a
new single-candidate work order must use the next evidence-backed bottleneck.

## Required follow-up

After an accepted candidate, run the targeted Ready Vote SLO,
Ready Vote saturation-v3 and read-concurrency-ramp regressions. The
`<1000 ms` target remains open until the original authenticated page contract
passes with HTML TTFB p95 below that threshold.
