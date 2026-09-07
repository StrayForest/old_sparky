# Archived authenticated HTML/TTFB candidate — 2026-09-07

Status: rejected as the `<1000 ms` target solution after exact production A/B.
Owner: Platform maintainers.

This work order continues the open authenticated first-byte target after the
rejected admission candidate archived in
[`performance-authenticated-html-ttfb-admission-2026-09-07.md`](performance-authenticated-html-ttfb-admission-2026-09-07.md).
It owns one bounded web read-path candidate and does not reopen the completed
13-profile performance matrix.

## Objective and evidence

Reduce `authenticated-page-load-v1` HTML TTFB p95 from the retained baseline
of `2568.859 ms` to `<1000 ms`, while preserving the read path, Ready Vote,
authentication/security semantics, no-JavaScript header behavior, backend
budget and exact fixture cleanup.

Fresh attribution and the rejected admission A/B showed the remaining
authenticated page path is dominated by two serialized requests: the root
layout waited for authoritative `/auth/bootstrap`, then the tournament page
loaded `/tournaments/{slug}/workspace`. In the rejected A/B, bootstrap p95 was
`917.773 ms` and workspace p95 was `1042.102 ms`, while CPU remained about
`96%` per core and SQL execution was not the dominant cost. The first merged
overlap candidate was then measured under the exact profile in run
`34145804669`: all `20,000/20,000` responses were HTTP 200 with exact cleanup,
but HTML TTFB p95 was `2527.624 ms`, only `1.605%` below the retained
`2568.859 ms` baseline, so it was not accepted as the target solution.

The diagnostic follow-up in run `34148261104` enabled only SSR stage logging.
It measured auth bootstrap p95 `899.81 ms`, workspace p95 `889.505 ms`, detail
data-ready p95 `1112.536 ms`, and unattributed upstream time after data-ready
p95 `2340.311 ms`. CPU stayed about `97%` per host core, with no lock waiters
and PostgreSQL peak `51`. This identifies the post-data Next response/render
and flush path as the next bounded bottleneck; production was restored to the
ordinary `ready-vote-static-8` profile after diagnostics.

## Previous candidate

For an initial queryless `/tournaments/{slug}` request, the root layout starts
the existing detail workspace request before awaiting its authoritative auth
bootstrap. The page consumes that same request-local React cache entry, so
the change overlaps the two existing reads and does not add a duplicate API
request. Requests with a query string, including invite-code links, keep the
existing sequential path and semantics.

The proxy overwrites two internal request headers with the actual pathname and
search string; the server uses them only to identify this exact initial detail
route. The root layout still awaits `/auth/bootstrap` before rendering
`AuthProvider` and `SiteHeader`, so authenticated identity, unavailable-session
handling and no-JavaScript output remain authoritative. No API endpoint,
authorization check, Ready Vote state, DB pool, worker count, admission limit,
retry policy, cache of session validity, CSP or private/no-store policy changes.

## Candidate tested

Keep the previous request overlap, but make the tournament detail page's outer
server component synchronous and place the existing data-dependent content
behind an explicit route-local `Suspense` boundary with the existing
`RouteLoadingShell`. This tests whether Next can flush the already-available
root layout/header and route fallback before the expensive post-data component
tree completes. The authoritative root-layout auth wait, the shared workspace
request, private/error handling, no-JavaScript authenticated header, API
contracts, Ready Vote state, security headers, and backend budgets are
unchanged.

## Production result

The candidate was deployed at exact source SHA
`d318e19e36b7356815e874827576f43e1485211a` through the reviewed `dev` chain
and measured by external run `34153656342`. The unchanged
`authenticated-page-load-v1` contract returned `20,000/20,000` HTTP 200
responses, zero errors, zero retries, zero overload responses, PostgreSQL
backend peak `51`, and exact cleanup. HTML TTFB p95/p99 was
`2325.559/3069.367 ms` (p50 `1429.945 ms`), a `9.47%` p95 improvement over
the retained `2568.859 ms` baseline, but still well above the `<1000 ms`
target. Nginx upstream-header p95 was `1901 ms`; sampled server request p95
was `1123.092 ms`, with DB SQL p95 `539.006 ms`, pool checkout p95
`438.047 ms`, and no lock waiters. The candidate is therefore a safe
incremental improvement, not target closure.

The remaining concrete bottleneck is post-auth/route web response generation
under load. The next active candidate defers the heavy interactive detail view
from SSR while retaining server workspace/auth reads and SSR header/hero.

## Local gates

- web-quality passes: dependency audit, typecheck, lint and production build;
- web-hermetic passes: 479 tests passed, 29 skipped;
- participant-progressive passes: 8 tests passed;
- authenticated tournament SSR smoke confirms one workspace request and no
  `/users/me` dependency;
- the four-project no-JavaScript/auth-state header smoke remains passing;
- `git diff --check` passes.

## A/B protocol

1. Push the reviewed candidate through the normal `dev` exact-SHA security,
   build and automatic production chain.
2. Run `authenticated-page-load-v1` with the unchanged 20,000-user,
   40-tournament fixture and exact cleanup.
3. Compare HTML TTFB/page p95 and p99, Nginx upstream header, root-layout auth,
   workspace, bootstrap, pool checkout/hold, SQL, CPU, PostgreSQL backends and
   locks, HTTP correctness, Ready Vote and cleanup.
4. Restore the ordinary `ready-vote-static-8` production runtime if the
   candidate fails to improve TTFB materially, produces any unexpected
   status/timeout/520/522, exceeds backend `52`, breaks Ready Vote/security/
   no-JavaScript behavior, duplicates the workspace request, or fails exact
   cleanup.

The candidate is accepted only if the same-contract production run improves
TTFB and passes all safety gates. The `<1000 ms` target remains open until
the original authenticated page contract reports TTFB p95 below that value.

## Follow-up

The `<1000 ms` target remains open. This work order is archived in favor of the
next evidence-backed candidate in
[`active-authenticated-html-ttfb-client-boundary-2026-09-07.md`](../../performance/active-authenticated-html-ttfb-client-boundary-2026-09-07.md).
