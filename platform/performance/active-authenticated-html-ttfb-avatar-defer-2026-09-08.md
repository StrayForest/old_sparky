# Active authenticated HTML/TTFB candidate — deferred bootstrap avatar — 2026-09-08

Status: candidate ready for reviewed production A/B. Owner: Platform
maintainers.

This work order follows the archived browser-workspace candidate and owns the
single next authenticated first-byte experiment. It does not reopen the
completed 13-profile performance matrix.

## Objective and fresh attribution

Reduce `authenticated-page-load-v1` HTML TTFB p95 from the retained
`2568.859 ms` baseline to `<1000 ms`, preserving the authorized read path,
Ready Vote, authentication/security semantics, no-JavaScript authenticated
header, backend budget and exact fixture cleanup.

The exact candidate A/B
[`34169435362`](https://github.com/StrayForest/old_sparky/actions/runs/34169435362)
returned `20,000/20,000` HTTP 200 responses with exact cleanup and HTML TTFB
p95 `1,595.286 ms` after deferring the workspace read. Its diagnostic repeat
[`34171146327`](https://github.com/StrayForest/old_sparky/actions/runs/34171146327)
measured upstream-header p95 `1,141 ms`, upstream-connect p95 `1 ms`, sampled
root auth-bootstrap p95 `95.222 ms` and sampled root component-tree p95
`96.373 ms`. API request-perf showed auth-bootstrap request p95 `988.956 ms`,
SQL p95 `586.067 ms`, pool checkout p95 `612.248 ms`, CPU about `91%` per
core, backend peak `44` and zero lock waiters. These are sampled diagnostic
quantiles, not additive latency components.

## Candidate

Keep the current authoritative auth query, session expiry/status checks, role
loading, credits, SSR identity, admin visibility and no-JavaScript header.
Call the existing bootstrap builder without its optional avatar projection so
the second profile/avatar SQL query is removed from the blocking request. The
header already renders a safe account icon when no avatar URL is available;
this experiment does not replace an authenticated user with an anonymous
fallback and does not add a cache for session validity.

The bounded UX trade-off is that the initial SSR header may show the safe icon
until a later profile surface supplies avatar data. Profile endpoints retain
their existing avatar behavior. Ready Vote, workspace authorization, session
validation, security headers, API contracts, worker count, pool limits and
PostgreSQL budget remain unchanged.

The candidate is rejected if it changes authorization/security behavior,
breaks header identity or no-JavaScript semantics, creates unexpected
statuses/timeouts/520/522 responses, exceeds backend `52`, fails exact
cleanup, worsens the accepted read contour, or does not close the `<1,000 ms`
target.

## Local and production gates

- direct web typecheck, lint and production build must pass;
- focused auth/bootstrap and web-hermetic smoke tests must pass;
- Python dependency gates must run in CI; a missing local safe dependency is
  `LOCAL GATE BLOCKED`, not a reduced pass;
- exact-SHA security/build, automatic production deployment and the unchanged
  `authenticated-page-load-v1` 20k/40-tournament A/B with exact cleanup are
  required;
- after a target-closing result, run Ready Vote SLO, saturation-v3 and
  read-concurrency-ramp regressions.
