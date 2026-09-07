# Archived authenticated HTML/TTFB investigation — 2026-09-07

Status: blocked after fresh attribution; target remains open. Owner: Platform maintainers.

The preceding production performance stage is closed. Its work order is
archived in [`performance-stage-request-2026-09-07.md`](performance-stage-request-2026-09-07.md),
and its canonical evidence is in
[`performance-stage-2026-09-07.md`](performance-stage-2026-09-07.md).
This work order owns only the remaining authenticated HTML first-byte target;
it does not reopen or duplicate the completed 13-profile matrix.

## Objective

Reduce `authenticated-page-load-v1` HTML TTFB p95 from `2568.859 ms` to
`<1000 ms` while preserving the read path, Ready Vote, authentication and
security semantics, no-JavaScript header behavior, and the PostgreSQL backend
budget.

## Current baseline

- measured production SHA: `bba3fb278e348906a6942aee8462b758c3d616ef`
- 20,000/20,000 authenticated HTML responses were HTTP 200
- page p95: `3357.939 ms`; HTML TTFB p95: `2568.859 ms`
- Nginx upstream connect p95: approximately `1 ms`; upstream header p95:
  `2228 ms`
- sampled `/auth/bootstrap` p95: `1278.7 ms`
- sampled DB p95: `538 ms`; pool checkout p95: `714 ms`
- API CPU: approximately `96.8%` per core; PostgreSQL backend peak: `51/52`
- unexpected statuses, timeouts, 520 and 522: `0`

These values locate the open investigation in the authenticated web/API path;
they do not authorize pool or worker scaling or assign a Cloudflare root cause.

## Exact-SHA attribution before code optimization

The retained exact-SHA production artifact for run
[`34096416799`](https://github.com/StrayForest/old_sparky/actions/runs/34096416799)
was re-read before this work. It used SHA `bba3fb278e348906a6942aee8462b758c3d616ef`,
the unchanged `authenticated-page-load-v1` contract, and completed 20,000/20,000
HTTP 200 responses with exact cleanup. Its bounded observer showed:

- client HTML TTFB p95 `2568.859 ms`; Nginx upstream-header p95 `2228 ms`;
  upstream-connect p95 `1 ms`;
- `/auth/bootstrap` sampled p95 `1278.7 ms`, average `2.0` SQL/request,
  DB SQL p95 `538.078 ms`, checkout p95 `714.126 ms`, and connection hold
  p95 `606.345 ms`;
- sampled API admission wait was `0 ms`; PostgreSQL had no lock waiters,
  backend max `51/52`, while CPU averaged `96.78–96.80%` per core;
- the same sample recorded workspace p95 `1451.416 ms`, average `3.014`
  SQL/request, checkout p95 `763.439 ms`, and connection hold p95
  `843.594 ms`.

The artifact's `ssr_perf` and event-loop populations were empty because the
production runtime used the baseline profile with SSR diagnostics disabled.
The bounded diagnostic window below filled that attribution gap. No
optimization is accepted from an unmeasured hypothesis.

## Required fresh attribution

Before any optimization, measure the current SHA with bounded,
production-safe diagnostics across:

```text
client → Cloudflare → Nginx → Next request → root layout
→ server auth bootstrap → API admission → DB checkout → auth/session SQL
→ avatar projection → DB release → remaining SSR → SiteHeader → first byte
```

Separate total HTML TTFB, Nginx connect/header time, Next SSR total,
root-layout auth wait, `/auth/bootstrap` total, authenticated admission wait,
DB checkout wait, SQL time/count, non-SQL bootstrap time, post-bootstrap SSR,
and event-loop/CPU pressure. Existing `PLATFORM_PERF_*`, `request_perf`,
`ssr_perf`, Nginx access timing and observer instrumentation are preferred.
New logs must be env-gated, sampled/bounded and secret-free.

The current instrumentation now adds three `/auth/bootstrap` stage fields:
authoritative auth SQL, minimal avatar projection SQL, and response-model
construction. It also reports non-SQL time after subtracting DB checkout,
SQL and measured compute, so connection hold can be compared with work after
the final SQL. These fields are diagnostic only and do not change auth flow.

## Fresh production attribution

The diagnostic deployment used reviewed production SHA
`a43f77b4f773a13ea7d6fd7748f23585ed05dbd8`, whose application code is
equivalent to the retained exact-SHA baseline, with the existing sampled SSR
and event-loop diagnostics enabled. The exact original external-load contract
was then run in
[`34125517721`](https://github.com/StrayForest/old_sparky/actions/runs/34125517721).
The diagnostic deployment was restored to the baseline profile by successful
run
[`34129165308`](https://github.com/StrayForest/old_sparky/actions/runs/34129165308).

The full 20,000-request population returned 20,000/20,000 HTTP 200 responses,
zero errors, unexpected statuses, retries, overload responses, timeouts, 520s
or 522s, and exact cleanup. The profile nevertheless reported `STRESS BEHAVIOR
FAIL` because total page p95 was `5799.507 ms` against its `5000 ms` stress
budget. HTML TTFB p95 was `4271.489 ms` (p99 `5092.385 ms`), so the
`<1000 ms` target remains open.

The independent p95 populations below are attribution evidence, not additive
latency components. The normalized percentages are each divided by the full
client HTML TTFB p95 and must not be summed.

| Component | Population | p95 | Normalized view |
| --- | ---: | ---: | ---: |
| Client HTML TTFB | 20,000 | `4271.489 ms` | `100%` |
| Nginx upstream connect | 20,000 | `1.000 ms` | `0.02%` |
| Nginx upstream header / Next first byte | 20,000 | `3904.000 ms` | `91.4%` |
| Client-to-Nginx-header gap, including client/Cloudflare path (derived) | 20,000 | `367.489 ms` | `8.6%` |
| `/auth/bootstrap` request total | 5,761 request-perf records | `1981.260 ms` | `46.4%` |
| Authenticated API admission wait | 5,761 request-perf records | `0 ms` | `0%` |
| `/auth/bootstrap` pool checkout wait | 5,761 request-perf records | `951.800 ms` | `22.3%` |
| `/auth/bootstrap` SQL time | 5,761 request-perf records | `314.622 ms` | `7.4%` |
| Root-layout auth bootstrap fetch | 200 sampled SSR requests | `1447.342 ms` | `33.9%` |
| Tournament data ready / post-bootstrap route work | 200 sampled SSR requests | `2288.759 ms` | `53.6%` |
| Event-loop lag metric | 249 observer samples | `188.115 ms` | pressure signal, not a request component |

The sampled correlated HTML records also show upstream-header p95
`4200.650 ms`, tournament workspace p95 `2287.312 ms`, and an independent
residual after sampled route data p95 `4847.601 ms`. That residual is not
additive to the table: the SSR and Nginx samples are correlated by request
identity but their quantiles are not a decomposition of one request's TTFB.
The observer recorded CPU at approximately `99.8%` on each of the two cores;
PostgreSQL CPU averaged `7.49%`, backend connections peaked at `51/52`, and
lock waiters stayed at `0`.

`pg_stat_statements` makes the two bootstrap SQL statements low-leverage
optimization targets in this window: the authoritative auth projection
averaged `0.150738 ms` over 20,000 calls and the minimal avatar projection
averaged `0.107912 ms` over 20,000 calls. The API service already releases its
DB session in `finally` immediately after the last bootstrap SQL; the newly
added per-stage and post-SQL fields are staged for the next reviewed candidate
window because this diagnostic SHA predates those branch changes.

### Rejected hypotheses and next candidate

- Cloudflare/network and Nginx connection setup are not the main bottleneck:
  connect p95 was `1 ms`, with no 520/522 responses.
- PostgreSQL lock contention, backend exhaustion and authenticated admission
  queuing were not observed. Pool checkout is materially elevated, but pool
  size/overflow and workers remain unchanged until a reviewed candidate proves
  safety.
- Merging the two bootstrap queries is not accepted: their measured database
  execution is sub-millisecond on average and cannot explain multi-second
  TTFB by itself.
- An anonymous fallback, global authoritative-session cache or whole-root
  Suspense boundary remains incompatible with the no-JavaScript header and
  security contract.

The next concrete candidate is a separately reviewed, same-contract A/B for a
bounded `/auth/bootstrap` service-priority path that reduces its observed pool
queue/CPU contention without increasing the DB pool, API workers, admission
limits or accepted error/timeout budgets. It must first deploy the staged
bootstrap fields, then repeat this exact profile and be rejected unless it
improves TTFB p95 while keeping Ready Vote, backend `<=52`, security and exact
cleanup unchanged. No candidate was deployed or accepted in this stage.

## Investigation order

1. Establish whether pool checkout queues across both API workers and measure
   connection hold time, SQL count/time, and work after the final SQL.
2. Verify that `/auth/bootstrap` remains authoritative and returns only the
   identity, required roles/permissions and credits, plus the minimal avatar
   URL needed by `SiteHeader`; never cache authoritative session validity.
3. If a connection remains checked out after the last SQL, materialize a
   detached result and release it before Python/media/serialization/SSR work.
4. Attribute the API CPU component before changing crypto, serialization, ORM,
   logging, middleware or event-loop work.
5. Attribute post-bootstrap Next SSR, including route data, render and header
   stages. The existing tournament-page `Promise.all` should remain unless a
   fresh trace proves a different waterfall.

Each candidate is one bounded change followed by the same-contract retest.
Record before/after total p95, TTFB p95, bootstrap, upstream header, pool
checkout, DB, CPU, response correctness and cleanup. Revert candidates without
reproducible improvement.

## Acceptance and regression

The authenticated target requires the original
`authenticated-page-load-v1` contract: 20,000 users, 40 tournaments,
20,000/20,000 HTTP 200, zero unexpected status/timeout/520/522, PostgreSQL
backends `<=52`, exact cleanup, and HTML TTFB p95 `<1000 ms`.

After the last accepted candidate, run targeted Ready Vote SLO, Ready Vote
saturation v3, and read-concurrency-ramp regressions. Preserve their existing
contracts and thresholds; do not repeat the full matrix unless this change
affects the shared runtime contour or the authenticated target is closed.

## Constraints

Do not change API workers, DB pool size/overflow, `pool_pre_ping`, Ready Vote
admission, retry policy, wait queues or acceptance thresholds without separate
evidence and authorization. Do not replace an authenticated header with an
anonymous fallback, add a whole-root Suspense boundary to hide latency, use a
global authoritative-session cache, or weaken private/no-store/CSP behavior.

The target remains open. This work order is closed as an explicit blocked
closeout after the final attribution above; its separately reviewed candidate
must reopen a new work order and repeat the exact acceptance contract.
