# Active authenticated HTML/TTFB candidate — client detail boundary — 2026-09-07

Status: candidate ready for reviewed production A/B. Owner: Platform
maintainers.

This work order continues the open authenticated first-byte target after the
safe but insufficient route-local Suspense candidate archived in
[`performance-authenticated-html-ttfb-prefetch-2026-09-07.md`](../docs/archive/performance-authenticated-html-ttfb-prefetch-2026-09-07.md).
It owns one bounded web read-path candidate and does not reopen the completed
13-profile performance matrix.

## Objective and evidence

Reduce `authenticated-page-load-v1` HTML TTFB p95 from the retained baseline
of `2568.859 ms` to `<1000 ms`, while preserving the read path, Ready Vote,
authentication/security semantics, no-JavaScript header behavior, backend
budget and exact fixture cleanup.

The previous exact production A/B run `34153656342` returned `20,000/20,000`
HTTP 200 responses with exact cleanup and no retries, errors, overload,
unexpected statuses or lock waiters. TTFB p95/p99 improved to
`2325.559/3069.367 ms`, while Nginx upstream-header p95 was `1901 ms` and
sampled server-request p95 was `1123.092 ms`. DB SQL p95 was `539.006 ms` and
pool checkout p95 `438.047 ms`, so the residual bottleneck remains web
response generation/flush rather than a database lock or pool admission
failure. The earlier SSR-only diagnostic run `34148261104` also measured
post-data unattributed upstream p95 `2340.311 ms`.

## Candidate

Keep the existing queryless detail workspace/auth overlap and route-local
Suspense fallback. Replace only the server-rendered `TournamentDetailView`
with a client-only dynamic boundary. The server still performs the
authoritative auth bootstrap and authorized workspace read, and still SSRs the
root header and tournament hero; the heavy interactive detail tree is loaded
after hydration from the same serialized workspace data. Ready Vote,
registration, bracket links, API contracts, private/error handling, security
headers, no-JavaScript authenticated header, worker count, pool limits and
PostgreSQL budget remain unchanged.

The bounded trade-off is that the interactive detail panel shows a small
loading state until its client chunk loads; the server header and route hero
remain available without JavaScript. The candidate is not accepted if it
changes authorization behavior, duplicates workspace requests, creates
unexpected statuses/timeouts/520/522, exceeds backend `52`, fails exact
cleanup, or does not materially reduce TTFB.

## Local gates

- direct web typecheck and lint pass;
- targeted detail route, auth header and dependency-graph smoke passes 12/12;
- web-quality passes: dependency audit, typecheck, lint and production build;
- web-hermetic passes: 479 tests passed, 29 skipped;
- participant-progressive passes: 8 tests passed;
- docs and verification-contract gates pass.

## A/B protocol

1. Push through the normal `dev` exact-SHA security/build and automatic
   production chain.
2. Run unchanged `authenticated-page-load-v1` with 20,000 users, 40
   tournaments and exact cleanup.
3. Compare HTML TTFB/page p95 and p99, Nginx upstream header, server/SSR
   timings, bootstrap/workspace, pool/SQL, CPU, PostgreSQL backends/locks,
   HTTP correctness, Ready Vote/security/no-JavaScript behavior and cleanup.
4. Keep the ordinary `ready-vote-static-8` runtime if the candidate is
   rejected; do not alter worker/pool budgets as part of this A/B.

The `<1000 ms` target remains open until the original authenticated page
contract reports TTFB p95 below that value. After an accepted target-closing
candidate, run the targeted Ready Vote SLO, saturation-v3 and read-concurrency
ramp regressions.
