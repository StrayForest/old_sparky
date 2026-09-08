# Authenticated HTML/TTFB candidate — global chrome provider boundary — 2026-09-08

Status: rejected after exact production A/B; reviewed rollback in progress.
Owner: Platform maintainers.

## Objective

Reduce `authenticated-page-load-v1` HTML TTFB p95 below `1,000 ms` while
preserving the authorized read path, Ready Vote, authentication/security
semantics, the server-rendered no-JavaScript authenticated header, the
`52` PostgreSQL backend ceiling and exact fixture cleanup.

The safe comparison point was the production `ready-vote-static-8` contour
with authoritative server `/auth/bootstrap`, browser-deferred tournament
workspace and optional avatar enrichment removed from the blocking bootstrap.
Its exact authenticated page result was run `34174264965`: `20,000/20,000`
HTTP 200 responses, HTML TTFB p95 `1,512.860 ms`, page p95 `1,868.297 ms`,
and exact cleanup.

## Attribution and candidate

A fresh diagnostic attempt on safe SHA `e59485a278d09b9a00ecee86358df3e8ea1d4175`
completed its origin supervisor and exact cleanup, but did not export a client
report or server-observability artifact. It is not performance evidence. The
candidate therefore used the stable existing attribution and changed only the
React provider boundary.

The root layout retained the authoritative auth bootstrap and passed its
initial state to `SiteHeader`, preserving authenticated identity, roles and
the profile link in SSR HTML without JavaScript. The existing browser auth
state bridge handled later header refresh/logout updates. Both `SiteHeader`
and `SiteFooter` were moved outside `AuthProvider`; the route subtree stayed
inside it and `I18nProvider` remained their parent. No API routes, SQL,
workspace authorization, Ready Vote, CSP, cookies, response-cache policy,
workers, database pools, admission controls or error budgets changed.

## Verification

- Candidate source SHA: `0cb0f1fabafa793d0520774a04883b02ad4a2584`.
- Exact-SHA security/build run: `34215322181`, all required checks passed.
- Production deployment: `34215966398`; target SHA and
  `ready-vote-static-8` profile were verified, including live smoke.
- Local canonical `web-quality` and `web-hermetic` passed, as did docs,
  verification-contract and `git diff --check` gates.

## Exact production A/B result

The unchanged `authenticated-page-load-v1` stress contract was used with
`20,000` users, `40` tournaments, `20,000` HTML responses, concurrency `64`,
and no retries. External run `34216385147` completed with full evidence:

| Metric | Result |
| --- | ---: |
| HTTP responses | `20,000/20,000` status `200` |
| Errors / unexpected / timeout / overload / retry | `0 / 0 / 0 / 0 / 0` |
| HTML TTFB p50 / p90 / p95 / p99 | `1082.680 / 1307.144 / 1391.415 / 1604.320 ms` |
| Total page p95 / p99 | `1704.678 / 1960.978 ms` |
| PostgreSQL backend peak | `44` (`<=52`) |
| Waiting backends / lock waiters | `1 / 0` |
| Web workers | `2` |
| CPU per core average / max | `86.80% / 100.00%` and `83.76% / 100.00%` |
| Web process average / max CPU | `96.30% / 120.56%` |
| Exact cleanup | `20,000` users and `40` tournaments deleted; all remnants `0` |

The run passed its stress safety contract, but the target gate failed:
TTFB p95 was `391.415 ms` above `<1,000 ms`. Compared with the safe avatar
baseline, TTFB p95 improved by `120.445 ms` (`7.96%`) but remained too slow;
the total page p95 was `163.619 ms` better. It was also worse than the
narrower provider-boundary candidate (`1,341.322 ms`, run `34192721178`).

Origin evidence shows no database safety regression or database saturation:
backend peak `44`, API ownership `40`, worker ownership `2`, observer `1`,
ownership mismatches `0`, lock waiters `0`, waiting backends `1`, PostgreSQL
CPU max `13.27%`, and exactly two web workers. The remaining bottleneck is
therefore still in the CPU-bound Next.js/SSR path, but this A/B cannot
attribute the exact component stage because the external observer reported
zero request-perf log samples. The bridge/provider-boundary hypothesis is
rejected as target-closing and is not a basis for capacity changes.

## Rollback and next candidate

The candidate is reverted through the reviewed `dev` path; production must
return to the safe `ready-vote-static-8` layout before the next experiment.
The rollback restores the original single `AuthProvider` boundary around the
route subtree and global chrome. The next candidate is diagnostic-first
stable-contour root component-tree work: first obtain exportable sampled SSR
stage timings from the exact production route, then change one blocking stage
with a measured attribution. Worker count, pool sizes, admission limits and
load thresholds remain unchanged.
