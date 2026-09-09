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
upstream/request p95 `2,619 ms`. The read-only runtime diagnostics
[34344738197](https://github.com/StrayForest/old_sparky/actions/runs/34344738197)
then identified the direct cause: the `deadlock-web` systemd cgroup has
`MemoryMax=1G`, and the kernel killed `next-server` twice at approximately
`1,029,412 KiB` and `1,029,144 KiB` resident memory. systemd recorded
`Result=oom-kill`, status `9/KILL`, followed by two automatic restarts. The
restart churn therefore explains the 502s and secondary queueing; it is not a
Cloudflare or Nginx buffering finding.

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

## Runtime candidate

The restart cause is confirmed, but the retained-memory owner still needs an
A/B. Do not raise `MemoryMax`, add workers, scale database pools or change the
React root flush boundary based on this evidence. The first focused candidate
is to bypass Next.js' patched server `fetch` for the two loopback auth GETs
used by the server-rendered shell. The candidate keeps the same trusted
loopback `/api/v1` URL policy, cookie and correlation headers, a total
2-second timeout, identity content encoding, a 256 KiB response limit and
bounded keep-alive agents (`maxSockets=128`, `maxFreeSockets=16`); existing
status handling and response validators remain unchanged.

The bounded read-only workflow
`platform-production-web-runtime-diagnostics.yml` collects, for an exact
UTC window and deployed SHA:

- systemd `Result`, exit status, restart count, memory peak/current and task
  limits;
- sanitized `deadlock-web` journal lines; and
- kernel OOM/cgroup kill events.

The OOM evidence is now collected; the auth transport candidate must first
pass build, security, correctness, capacity and exact cleanup gates, then be
measured in the same canonical external profile. Compression remains enabled
because the same-source A/B already rejected disabling it.
