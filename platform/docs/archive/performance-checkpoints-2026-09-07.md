# Performance checkpoint: 2026-09-07

- Status: Historical
- Owner: Platform performance
- Last reviewed: 2026-11-23

## Historical production performance stage (2026-09-07)

The 2026-09-07 performance stage is closed historical evidence with an
explicit authenticated HTML follow-up. The exact 13-profile repeat used the
then-current contracts and thresholds on deployed SHA
`bba3fb278e348906a6942aee8462b758c3d616ef`; lifecycle profiles remain
preproduction-only. The full evidence table, run links, change ledger,
origin-safety results and cleanup proof are in the archived
[2026-09-07 performance-stage report](../archive/performance-stage-2026-09-07.md).
Do not treat this matrix as the current portfolio or as evidence that the
current timing, observer-binding or Redis projection cleanup contract was
executed.

The historical run reported all 13 profiles passing their then-declared
acceptance and exact cleanup. Backend
peaks were 51–52 against the 52-backend budget. Normal Ready Vote/read traffic
had no unexpected statuses, timeouts, 520s or 522s. Stress profiles returned
only their declared controlled `503` overload responses, while read profiles
completed their 200/304 contracts without errors or retries.
That historical cleanup result is preserved as reported; it is not
retroactively upgraded to proof that the current per-key Redis projection
cleanup deleted `teams`, `workspace_detail`, `bracket_summary` and
`bracket_full` with zero residual keys.

The authenticated page returned 20,000/20,000 HTTP 200 responses, with total
p95 `3357.939 ms` and HTML TTFB p95 `2568.859 ms`. The D9 shell projection is
functionally clean, but this run is not a same-window unchanged-code A/B and
the requested authenticated TTFB target `<1,000 ms` remains open. The final
attribution for the bounded authenticated HTML/TTFB investigation is archived
in
[`performance-authenticated-html-ttfb-2026-09-07.md`](../archive/performance-authenticated-html-ttfb-2026-09-07.md);
the browser-workspace candidate is archived in
[`performance-authenticated-html-ttfb-workspace-client-2026-09-08.md`](../archive/performance-authenticated-html-ttfb-workspace-client-2026-09-08.md),
the avatar-defer candidate is archived in
[`performance-authenticated-html-ttfb-avatar-defer-2026-09-08.md`](../archive/performance-authenticated-html-ttfb-avatar-defer-2026-09-08.md),
and the rejected chrome-boundary candidate is archived in
[`performance-authenticated-html-ttfb-chrome-boundaries-2026-09-08.md`](../archive/performance-authenticated-html-ttfb-chrome-boundaries-2026-09-08.md).
The rejected admission candidate and its exact A/B result are archived in
[`performance-authenticated-html-ttfb-admission-2026-09-07.md`](../archive/performance-authenticated-html-ttfb-admission-2026-09-07.md).
The first merged overlap candidate was deployed in release
`4db0079fcddba6fa37bd089d2a96599c04d02768` and measured by exact external run
`34145804669`: all `20,000/20,000` HTML responses were HTTP 200 with exact
cleanup, but TTFB p95 was `2527.624 ms`, only `1.605%` below baseline. The
follow-up SSR-only diagnostic run `34148261104` measured post-data
unattributed upstream p95 `2340.311 ms` after detail data-ready p95
`1112.536 ms`; production was restored to ordinary `ready-vote-static-8`.
The archived route-local Suspense candidate improved TTFB p95 to
`2325.559 ms` in exact run `34153656342` but did not close the target. The
rejected client detail-boundary candidate was measured by exact external run
`34159422212`: it returned `20,000/20,000` HTTP 200 responses with exact
cleanup and TTFB p95 `2270.945 ms`, a safe incremental result that did not
close the target. The archived candidate deferred the tournament workspace
read itself to the browser while retaining the server-rendered authoritative
auth/header path. Its exact external A/B `34169435362` measured HTML TTFB p95
`1,595.286 ms` with `20,000/20,000` HTTP 200 responses and exact cleanup. The
diagnostic repeat `34171146327` measured upstream-header p95 `1,141 ms`,
upstream-connect p95 `1 ms`, sampled root auth-bootstrap p95 `95.222 ms` and
sampled root component-tree p95 `96.373 ms`; production was restored to
`ready-vote-static-8` by `34172262955`. The avatar candidate removed only the
optional avatar SQL from the blocking bootstrap request. The chrome-boundary
candidate was rejected after exact runs `34181055402` and `34182385484` both
failed to complete the 20k/40 client load; both fixtures were cleaned by
guarded abort/cleanup workflows. A post-rollback diagnostic deployment
`34185100223` and load `34185320306` repeated the pre-report stall; guarded
abort `34186422087` and exact cleanup `34186465537` passed with 20,000 users,
40 tournaments and zero remnants. Production was restored to
`ready-vote-static-8` by deploy `34184805970` for reviewed SHA
`3d7aca0e832bac3c79e1b391e7817b74e5695b03`. The root component-tree
diagnostic is archived in
[the measured root component-tree report](../archive/performance-authenticated-html-ttfb-root-component-tree-2026-09-08.md).
The root render/flush boundary proposal
[`proposed-authenticated-html-ttfb-root-render-flush-2026-09-09.md`](../../performance/proposed-authenticated-html-ttfb-root-render-flush-2026-09-09.md)
is frozen pending transport evidence; no capacity-setting change is
authorized. The reviewed transport-observability package is deployed at
source SHA `1d25ae335f3eb749ce36b4fe97f868d5f357803c` through production deploy
[`34325036735`](https://github.com/StrayForest/old_sparky/actions/runs/34325036735).
Its Nginx policy, Node event-loop/CPU/GC diagnostics and bounded hop probe are
diagnostic instrumentation only until the unchanged authenticated external
control is rerun. At that checkpoint, the documented follow-up was the
[`authenticated HTML transport runbook`](../performance-transport-runbook.md).
The source also exposes the isolated operator profile
`web-ssr-native-transport`, which changes only the trusted loopback auth GET
transport while retaining the one-worker baseline contour. The existing
`web-ssr-workers-2` profile remains a combined worker-plus-transport candidate;
neither profile is the production default without a clean, same-contour A/B.
The Next.js 16.3.4 security baseline
and its new unchanged control are archived in
[`security-web-dependencies-next-2026-09-09.md`](../archive/security-web-dependencies-next-2026-09-09.md).
The control is run `34287694375` with TTFB p95 `1725.436 ms` and full-page
p95 `2279.541 ms`; it replaces the historical 16.2.12 result as the only
valid control for a future A/B. The narrower authenticated-provider-boundary candidate is
archived in
[`performance-authenticated-html-ttfb-provider-boundary-2026-09-08.md`](../archive/performance-authenticated-html-ttfb-provider-boundary-2026-09-08.md).
It moved only `SiteFooter` outside `AuthProvider`, preserving the authoritative
server-rendered authenticated header and all API, SQL, worker, pool, admission,
permission and security behavior. Reviewed SHA
`6e3a6325a0c6bee025297a8c8edb00817faafb75` completed the exact 20k/40 profile
in [run 34192721178](https://github.com/StrayForest/old_sparky/actions/runs/34192721178):
20,000/20,000 HTTP 200, zero errors/unexpected/overload/retry responses,
HTML TTFB p95 `1341.322 ms`, total page p95 `1665.096 ms`, PostgreSQL peak
`43/52`, zero lock waiters, two workers and exact cleanup. This is a `47.8%`
TTFB p95 improvement from the unchanged `2568.859 ms` control but still misses
the `<1,000 ms` target by `341.322 ms`, so the candidate was rejected and
reverted through the reviewed `dev` path. The follow-up global-chrome
boundary candidate is archived in
[`performance-authenticated-html-ttfb-global-chrome-2026-09-08.md`](../archive/performance-authenticated-html-ttfb-global-chrome-2026-09-08.md).
Its reviewed source SHA `0cb0f1fabafa793d0520774a04883b02ad4a2584` completed
the exact 20k/40 profile in [run 34216385147](https://github.com/StrayForest/old_sparky/actions/runs/34216385147):
20,000/20,000 HTTP 200, zero errors/unexpected/timeouts/overload/retries,
HTML TTFB p95 `1391.415 ms`, total page p95 `1704.678 ms`, PostgreSQL peak
`44/52`, zero lock waiters, two workers and exact cleanup. Origin safety passed,
but the candidate missed the target by `391.415 ms` and was worse than the
narrower provider-boundary candidate, so it was rejected and reverted through
the reviewed `dev` path. Production was restored to `ready-vote-static-8` by
automatic deploy `34234377917` for rollback SHA
`fe48877e3c7e10549bdd4a50afad5d5a9744b2f6`. The tournament-detail-footer
candidate is archived in
[`performance-authenticated-html-ttfb-detail-footer-2026-09-08.md`](../archive/performance-authenticated-html-ttfb-detail-footer-2026-09-08.md).
Its exact load `34230327945` reached the full 20k/40 fixture but failed closed
on a client `IncompleteRead` before producing a TTFB report; exact cleanup
passed with zero remnants. At that checkpoint, the frozen root render/flush
proposal was the only remaining TTFB work order; no capacity-setting change
was authorized. The current performance disposition and any open follow-up are
in [`CURRENT.md`](../CURRENT.md).
The execution record remains available in
[the archived 2026-09-07 work order](../archive/performance-stage-request-2026-09-07.md), now closed
with that explicit follow-up.
