# Active authenticated HTML/TTFB candidate — browser workspace fetch — 2026-09-08

Status: candidate ready for reviewed production A/B. Owner: Platform
maintainers.

This work order owns the single remaining authenticated first-byte candidate.
It follows the rejected client detail-boundary candidate archived in
[`performance-authenticated-html-ttfb-client-boundary-2026-09-07.md`](../docs/archive/performance-authenticated-html-ttfb-client-boundary-2026-09-07.md)
and does not reopen the completed 13-profile performance matrix.

## Objective and fresh attribution

Reduce `authenticated-page-load-v1` HTML TTFB p95 from the retained
`2568.859 ms` baseline to `<1000 ms`, while preserving the authorized read
path, Ready Vote, authentication/security semantics, no-JavaScript
authenticated header, backend budget and exact fixture cleanup.

The exact diagnostic repeat `34161768785` against the current production
application source returned `20,000/20,000` HTTP 200 responses with exact
cleanup. External HTML TTFB p95 was `2133.759 ms`; Nginx upstream-header p95
was `1821.7 ms`. Correlated SSR samples measured authoritative root auth
bootstrap p95 `919.267 ms`, tournament workspace p95 `1010.276 ms`, detail
data-ready p95 `1027.663 ms`, and unattributed upstream time after data-ready
p95 `2188.932 ms`. API request-perf showed server p95 `1083.15 ms`, DB SQL
p95 `503.488 ms`, pool checkout p95 `498.694 ms`, and no authenticated
admission wait. API CPU remained about `96%` per core; PostgreSQL backends
peaked at `51` against the `52` budget, with no lock waiters. The diagnostic
contour was restored to `ready-vote-static-8` by `34163426494`.

These measurements identify the server-rendered workspace/detail path as the
remaining serialized dependency after the required auth bootstrap. The
quantiles are attribution evidence, not additive latency components.

## Candidate

Keep the root layout's authoritative `/auth/bootstrap` request and its
server-rendered `SiteHeader`, including authenticated identity and
no-JavaScript header behavior. Make the tournament detail server page resolve
only its route parameters and render a client loader. After hydration, the
browser requests the same authorized workspace endpoint with the existing
detail shape: `participants_limit=0`, `workspace_view=detail`,
`include_current_user=false`, and the normalized invite code when present.
The existing client-only heavy detail boundary then renders the mapped
workspace data.

The bounded UX trade-off is that the tournament detail hero/content and
invite/error state wait for JavaScript and the browser workspace request; the
authenticated header remains available from SSR without JavaScript. The
browser loader aborts obsolete requests and ignores stale generations. A
workspace `401/403` still enters the invite-code flow, `404` remains not-found,
and transient failures expose a localized retry. Ready Vote actions,
registration, bracket links, workspace authorization, session validation,
security headers, API contracts, worker count, pool limits and PostgreSQL
budget remain unchanged.

The candidate is rejected if it changes authorization or security behavior,
duplicates workspace requests in production, creates unexpected statuses,
timeouts, 520/522 responses, exceeds backend `52`, fails exact cleanup, or
does not materially reduce TTFB.

## Local gates

- direct web typecheck, lint and production build pass;
- targeted authenticated tournament, invite/error and header smoke pass;
- web-hermetic must pass the full smoke and participant-progressive suites;
- docs and verification-contract gates must pass;
- `git diff --check` must pass.

The hermetic suite is deterministic and does not silently retry. A parallel
viewport-only test flake is recorded separately from candidate regressions and
must be rerun before publication.

## A/B protocol

1. Push through the normal `dev` exact-SHA security/build and automatic
   production chain.
2. Run unchanged `authenticated-page-load-v1` with 20,000 users, 40
   tournaments and exact cleanup.
3. Compare HTML TTFB/page p95 and p99, Nginx upstream header, auth/bootstrap
   and workspace request counts, server/SSR timings, pool/SQL, CPU,
   PostgreSQL backends/locks, HTTP correctness, Ready Vote/security and
   cleanup.
4. Keep the ordinary `ready-vote-static-8` runtime if rejected; do not alter
   worker, pool, admission or error budgets as part of this A/B.

The `<1000 ms` target remains open until the original authenticated page
contract reports TTFB p95 below that value. After a target-closing result, run
the targeted Ready Vote SLO, saturation-v3 and read-concurrency ramp
regressions.
