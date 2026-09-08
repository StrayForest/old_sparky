# Archived authenticated HTML/TTFB candidate — client detail boundary — 2026-09-07

Status: rejected as the `<1000 ms` target solution after exact production A/B.
Owner: Platform maintainers.

This work order followed the route-local Suspense candidate archived in
[`performance-authenticated-html-ttfb-prefetch-2026-09-07.md`](performance-authenticated-html-ttfb-prefetch-2026-09-07.md).
It owned one bounded web read-path candidate and did not reopen the completed
13-profile performance matrix.

## Objective and candidate

The objective was to reduce `authenticated-page-load-v1` HTML TTFB p95 from
the retained `2568.859 ms` baseline to `<1000 ms`, while preserving the read
path, Ready Vote, authentication/security semantics, no-JavaScript header,
backend budget and exact fixture cleanup.

The candidate retained the queryless server-side auth/workspace overlap and
route-local fallback, but moved only the heavy interactive
`TournamentDetailView` behind the existing client-only dynamic boundary. The
authoritative auth bootstrap, authorized workspace read, root header, route
hero, API contracts, Ready Vote state, security headers, worker/pool limits
and private/error handling were unchanged.

## Production result

Reviewed source SHA
`d907b8ab5ab46ae26768e5a2e0910ea89ecce464` was measured by the unchanged
exact external run `34159422212`. The run returned `20,000/20,000` HTTP 200
responses with zero errors, retries or overload responses and exact cleanup.
HTML TTFB p95/p99 was `2270.945/3771.746 ms`; total page p95 was
`3057.842 ms`; average response size was `32771 bytes`.

The sampled server-request p95 was `1164.592 ms`, DB SQL p95 `522 ms`, pool
checkout p95 `518 ms` and connection hold p95 `742 ms`; Nginx upstream-header
p95 was `1994 ms`. The candidate was a safe incremental improvement, but it
did not approach the `<1000 ms` target and is rejected as the target solution.
Production was restored to the ordinary `ready-vote-static-8` contour.

## Follow-up

The next active candidate moves the tournament workspace read itself to the
browser while retaining the server-rendered authoritative auth/header path:
[`performance-authenticated-html-ttfb-workspace-client-2026-09-08.md`](performance-authenticated-html-ttfb-workspace-client-2026-09-08.md).
