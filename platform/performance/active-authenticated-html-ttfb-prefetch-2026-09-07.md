# Active authenticated HTML/TTFB candidate — 2026-09-07

Status: candidate ready for reviewed production A/B. Owner: Platform
maintainers.

This work order continues the open authenticated first-byte target after the
rejected admission candidate archived in
[`performance-authenticated-html-ttfb-admission-2026-09-07.md`](../docs/archive/performance-authenticated-html-ttfb-admission-2026-09-07.md).
It owns one bounded web read-path candidate and does not reopen the completed
13-profile performance matrix.

## Objective and evidence

Reduce `authenticated-page-load-v1` HTML TTFB p95 from the retained baseline
of `2568.859 ms` to `<1000 ms`, while preserving the read path, Ready Vote,
authentication/security semantics, no-JavaScript header behavior, backend
budget and exact fixture cleanup.

Fresh attribution and the rejected admission A/B show the remaining
authenticated page path is dominated by two serialized requests: the root
layout waits for authoritative `/auth/bootstrap`, then the tournament page
loads `/tournaments/{slug}/workspace`. In the rejected A/B, bootstrap p95 was
`917.773 ms` and workspace p95 was `1042.102 ms`, while CPU remained about
`96%` per core and SQL execution was not the dominant cost. The complete
attribution and admission result are retained in the linked archive reports.

## Candidate

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

## Local gates

- web typecheck passes;
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
4. Restore `authenticated-read-admission-32` if the candidate fails to improve
   TTFB materially, produces any unexpected status/timeout/520/522, exceeds
   backend `52`, breaks Ready Vote/security/no-JavaScript behavior, duplicates
   the workspace request, or fails exact cleanup.

The candidate is accepted only if the same-contract production run improves
TTFB and passes all safety gates. The `<1000 ms` target remains open until
the original authenticated page contract reports TTFB p95 below that value.

## Required follow-up

After an accepted candidate, run the targeted Ready Vote SLO, Ready Vote
saturation-v3 and read-concurrency-ramp regressions. If this candidate is
rejected, archive this work order and create one new work order for the next
evidence-backed bottleneck.
