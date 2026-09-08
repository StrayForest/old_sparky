# Active authenticated HTML/TTFB candidate — global chrome provider boundary — 2026-09-08

Status: candidate ready for reviewed production A/B. Owner: Platform
maintainers.

## Objective

Reduce the authenticated `authenticated-page-load-v1` HTML TTFB p95 below
`1,000 ms` while preserving the authorized read path, Ready Vote,
authentication/security semantics, the server-rendered no-JavaScript
authenticated header, the `52` PostgreSQL backend ceiling and exact fixture
cleanup.

The retained baseline is the safe production contour with the authoritative
server `/auth/bootstrap`, browser-deferred tournament workspace and optional
avatar enrichment removed from the blocking bootstrap. Its exact authenticated
page result was run `34174264965`: `20,000/20,000` HTTP 200 responses, HTML
TTFB p95 `1,512.860 ms`, page p95 `1,868.297 ms`, and exact cleanup.

## Fresh attribution status

A fresh diagnostic deployment/load was attempted on the current safe SHA
`e59485a278d09b9a00ecee86358df3e8ea1d4175` using only the reviewed SSR
sampling profile. The origin supervisor completed and exact cleanup removed
`20,000` users and `40` tournaments with zero remaining users, tournaments,
sessions or audit rows, but the client-report artifact was not exported. It is
not used as performance evidence. The current safe contour remains the
comparison point until this candidate produces a valid full-population report.

## Candidate

Keep the root layout's authoritative auth bootstrap and pass its initial state
to `SiteHeader`, so authenticated identity, roles and profile link remain in
SSR HTML without JavaScript. Add the existing request-local auth-state bridge
needed for header refresh/logout updates. Move both `SiteHeader` and
`SiteFooter` outside the `AuthProvider`; keep the route subtree inside it.
`I18nProvider` remains the parent of all three branches.

This changes only the React provider boundary. API routes, SQL, workspace
authorization, Ready Vote, CSP, cookies, response cache policy, workers,
database pools, admission controls and error budgets are unchanged. The
candidate must not introduce a duplicate `/users/me` or workspace request.

## Local gates

- canonical `web-quality` (dependency audit, typecheck, lint, production build);
- canonical `web-hermetic`, including participant-progressive and no-JS auth
  header coverage;
- `git diff --check`, docs and verification-contract gates.

## Production A/B acceptance

Use the unchanged `authenticated-page-load-v1` profile with `20,000` users,
`40` tournaments and `20,000` HTML responses. Accept only if all of the
following hold:

- `20,000/20,000` HTTP 200;
- timeout, unexpected status, 520, 522, overload and retry counts are zero;
- TTFB p95 is `<1,000 ms` and page p95 materially decreases;
- PostgreSQL backend peak is `<=52` with no safety regression;
- Ready Vote and security checks remain clean;
- exact cleanup reports zero fixture remnants.

If any gate fails or the result is not target-closing, revert through the
reviewed `dev` path and archive this candidate with its exact metrics. Do not
change worker, pool, admission or load thresholds to obtain a pass.
