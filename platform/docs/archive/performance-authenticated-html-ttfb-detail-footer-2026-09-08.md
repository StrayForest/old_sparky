# Authenticated HTML/TTFB candidate — tournament detail footer defer — 2026-09-08

Status: rejected after exact production load; reviewed rollback complete.
Owner: Platform maintainers.

## Objective

Reduce `authenticated-page-load-v1` HTML TTFB p95 below `1,000 ms` while
preserving the authorized read path, Ready Vote, authentication/security,
server-rendered no-JavaScript authenticated header, the `52` PostgreSQL
backend ceiling and exact fixture cleanup.

The safe production contour was `ready-vote-static-8`, with the authoritative
server `/auth/bootstrap` path and the accepted workspace/avatar optimizations
already deployed. The unchanged control was the retained baseline of
`2568.859 ms` HTML TTFB p95.

## Candidate and bounded change

The candidate was reviewed source SHA
`08438242b1dc1ddbe620e7b9d831f0e506c2bf9a` (PR [#57](https://github.com/StrayForest/old_sparky/pull/57),
commit `cf2d64ea894b2a07f7affce213a49410833452fa`). It deferred only
`SiteFooter` from server rendering on the exact `/tournaments/{slug}` detail
route. The footer remained unchanged for other routes and was loaded by the
browser after hydration. The authoritative auth bootstrap, authenticated
header, workspace read/authorization, Ready Vote, CSP, cookies, API/SQL,
workers, pools, admission controls and error budgets were unchanged.

Local web-quality and focused smoke checks passed. Exact-SHA CI run
`[34229131292](https://github.com/StrayForest/old_sparky/actions/runs/34229131292)`
passed all required security/build, backend, migration, web, docs and
verification gates. Production deploy
`[34229818876](https://github.com/StrayForest/old_sparky/actions/runs/34229818876)`
installed the candidate with runtime profile `ready-vote-static-8` and passed
live smoke.

## Exact production load result

The unchanged `authenticated-page-load-v1` contract used `20,000` users,
`40` tournaments, concurrency `64`, no retries and a required `20,000`
authenticated HTML response population. External run
`[34230327945](https://github.com/StrayForest/old_sparky/actions/runs/34230327945)`
reached the full fixture barrier and origin supervisor, but the external HTTP
client failed while reading a chunked response:

```text
http.client.IncompleteRead: IncompleteRead(6849 bytes read)
```

The client report was not produced, so there is no valid client TTFB
percentile or accepted HTTP-response count for this candidate. The external
load acceptance gate failed closed. The origin observer is supporting
operational evidence only: it recorded 20,000 Nginx HTML requests, upstream
connect p95 `1 ms`, upstream-header p95 `1,041 ms`, request-time p95
`1,440 ms`, PostgreSQL backend peak `37`, waiting backends peak `1`, lock
waiters `0`, ownership mismatches `0`, two web workers, and CPU per-core
averages/maxima of `86.62%/100%` and `82.56%/100%`. These timings do not
replace the missing client report.

Exact cleanup passed in the same run: `20,000` users and `40` tournaments
were deleted, with zero remaining users, tournaments, sessions or audit logs.
The retained observer artifact is available from the run's artifact
`10058346748`.

The candidate is rejected because response integrity and the required exact
load acceptance were not proven; the `<1,000 ms` target was not achieved or
measured. No capacity-setting change is authorized by this result.

## Rollback and next candidate

The candidate was reverted through reviewed PR [#58](https://github.com/StrayForest/old_sparky/pull/58).
The rollback merge SHA was `fe48877e3c7e10549bdd4a50afad5d5a9744b2f6`.
Exact-SHA CI run
`[34233603185](https://github.com/StrayForest/old_sparky/actions/runs/34233603185)`
passed all required gates. Automatic production deploy
`[34234377917](https://github.com/StrayForest/old_sparky/actions/runs/34234377917)`
restored `ready-vote-static-8`, installed release
`/opt/oldsparky/platform/releases/gha-34234377917-1-20260908T134950Z`, and
passed preflight and live smoke.

The next candidate remains diagnostic-first stable-contour root component-tree
work: first export sampled SSR stage timings from a complete exact load, then
change one blocking stage with measured attribution. Worker count, pool sizes,
admission limits and load thresholds remain unchanged.
