# Authenticated HTML TTFB transport investigation — 2026-09-09

- Status: Evidence archive; target not closed
- Owner: Platform maintainers
- Scope: Authenticated `/tournaments/[slug]` HTML under the canonical
  `authenticated-page-load-v1` production profile

## Result

The `<1,000 ms` authenticated HTML TTFB target remains open. The two new
experiments did not produce an accepted optimization:

- The unchanged `ready-vote-static-8` control, run
  [34334229164](https://github.com/StrayForest/old_sparky/actions/runs/34334229164),
  produced `16,461` HTTP 200 responses and `3,539` HTTP 502 responses out of
  20,000 requests. TTFB p95/p99 was `2,125.703/2,746.580 ms`. Exact fixture
  cleanup passed and left zero retained users, tournaments, sessions or audit
  rows.
- During that control the `deadlock-web` process disappeared and was replaced
  twice, at approximately `09:36:26–09:36:30` and `09:39:37–09:39:42 UTC`.
  The observer recorded two missing and two new web processes. The control is
  therefore diagnostic evidence, not a successful latency acceptance run.

The full server evidence was collected after the control window. It showed no
database lock contention, PostgreSQL backend ownership within the existing
budget, and no SSR diagnostics because the runtime profile was clean. Nginx
records for successful HTML responses had upstream-header p95 `1,924 ms` and
upstream/request p95 `2,619 ms`; the direct cause of the web-process churn was
not captured by the current observer.

## Transport and compression decisions

The read-only Cloudflare audit
[34326984588](https://github.com/StrayForest/old_sparky/actions/runs/34326984588)
reported response-body buffering disabled and no response-body-buffering rule.
The Nginx HTML contour already has buffering disabled and records both
upstream and client transport headers. Cloudflare is not currently supported
as the primary explanation for the missing latency.

A same-source compression candidate was built and deployed with Next.js
compression disabled. External run
[34339014480](https://github.com/StrayForest/old_sparky/actions/runs/34339014480)
returned `16,833` HTTP 200, `3,108` HTTP 502 and `59` client status-0 results.
TTFB p95/p99 worsened to `2,829.887/5,542.920 ms`; web CPU averaged about
`95%`, and Nginx observed no upstream content encoding. Compression disabled
is rejected. Production was restored to the default compressed artifact by
[34340974394](https://github.com/StrayForest/old_sparky/actions/runs/34340974394),
with preflight and live smoke checks passing.

## Remaining bottleneck investigation

The next measurement must identify why `deadlock-web` restarts during the
authenticated load. The bounded read-only workflow
`platform-production-web-runtime-diagnostics.yml` collects, for an exact
UTC window and deployed SHA:

- systemd `Result`, exit status, restart count, memory peak/current and task
  limits;
- sanitized `deadlock-web` journal lines; and
- kernel OOM/cgroup kill events.

Until that evidence is collected, do not change `MemoryMax`, worker counts,
database pool limits, Cloudflare response buffering or the React root flush
boundary. A web crash/restart or its health-check behavior can account for
the 502s and queueing, while the compression A/B already rules out the narrow
first-chunk compression hypothesis as a fix.
