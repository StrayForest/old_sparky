# Active authenticated HTML/TTFB candidate — detail footer hydration — 2026-09-08

Status: in progress. Owner: Platform maintainers.

## Objective

Reduce `authenticated-page-load-v1` HTML TTFB p95 below `1,000 ms` while
preserving the authoritative server auth bootstrap, server-rendered
authenticated header without JavaScript, browser workspace authorization,
Ready Vote behavior, CSP, the unchanged two-worker/52-backend contour and
exact fixture cleanup.

## Attribution and bounded change

The fresh diagnostic deployment and exact load `34222771502` showed:

- root auth/bootstrap and root component-tree sampled p95 values near `70 ms`;
- Nginx upstream connect p95 near `1 ms`, while upstream-header p95 was
  approximately `1,128 ms`;
- web CPU averaged about `96%` per process and PostgreSQL stayed below the
  `52` backend budget with zero lock waiters.

The candidate removes only the server-rendered footer from the tournament
detail document. `RouteSiteFooter` keeps the footer server-rendered for every
other route and loads the unchanged footer client-side for the exact
`/tournaments/[slug]` route. The header, auth provider, route loading shell,
workspace request, API contracts and security policy are unchanged. A
JavaScript-disabled detail document therefore intentionally keeps the
authenticated header but does not include the non-essential footer; hydrated
users receive the same footer component and links.

## Gates and measurement

Before production measurement, run canonical web quality, hermetic browser
tests, participant progressive tests, docs and verification-contract checks.
Then use the unchanged exact `authenticated-page-load-v1` contract: `20,000`
users, `40` tournaments, `20,000` HTML responses, `200` responses for all
requests, zero errors/unexpected statuses/timeouts/520/522, PostgreSQL
backends `<=52`, and exact cleanup. Reject the candidate and restore
`ready-vote-static-8` if the target or any safety condition fails.
