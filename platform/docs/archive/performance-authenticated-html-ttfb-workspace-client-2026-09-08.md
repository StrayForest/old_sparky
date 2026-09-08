# Archived authenticated HTML/TTFB candidate — browser workspace fetch — 2026-09-08

Status: rejected as the target-closing change. Owner: Platform maintainers.

The candidate kept the root layout's authoritative `/auth/bootstrap` request
and server-rendered `SiteHeader`, including authenticated identity and the
no-JavaScript header. It removed the tournament workspace read from the
server-rendered detail page and loaded the same authorized workspace shape in
the browser after hydration. Workspace authorization, Ready Vote, API
contracts, CSP, worker count, pool limits and PostgreSQL budgets were
unchanged.

The primary exact production A/B on source SHA
`20e907c9ac32d4f56deeb71b90037a88cc4a8363` was run as
[`34169435362`](https://github.com/StrayForest/old_sparky/actions/runs/34169435362):

| Population | Requests | p95 ms | p99 ms |
| --- | ---: | ---: | ---: |
| HTML page | 20,000 | 1,964.330 | 5,461.587 |
| HTML TTFB | 20,000 | 1,595.286 | 5,207.393 |

All `20,000/20,000` responses were HTTP 200, with zero errors, unexpected
statuses, retries, 520/522 responses and shedding. PostgreSQL backends peaked
at `43` (`40` API, `2` worker, `1` observer), lock waiters stayed at zero and
exact cleanup removed all `20,000` users and `40` tournaments while
preserving the control account.

The diagnostic repeat
[`34171146327`](https://github.com/StrayForest/old_sparky/actions/runs/34171146327)
used the reviewed sampled `web-ssr-diagnostics` profile on the same source.
It measured HTML TTFB p95 `1,412.661 ms`, Nginx upstream-header p95
`1,141 ms`, upstream-connect p95 `1 ms`, sampled root auth-bootstrap p95
`95.222 ms` and sampled root component-tree p95 `96.373 ms`. The API sample
showed auth-bootstrap request p95 `988.956 ms`, SQL p95 `586.067 ms` and pool
checkout p95 `612.248 ms`; CPU averaged about `91%` per core, with a backend
peak of `44` and no lock waiters. Exact cleanup passed. The diagnostic profile
was restored to `ready-vote-static-8` by
[`34172262955`](https://github.com/StrayForest/old_sparky/actions/runs/34172262955).

The candidate is a safe, material improvement over the retained baseline but
does not meet the required `<1,000 ms` TTFB p95. The fresh evidence moves the
remaining focus away from the server workspace waterfall: Nginx connect is
negligible, sampled Next stage work is below `100 ms`, and the remaining
production tail is queue/CPU pressure across the single-process Next and API
contour. The next bounded candidate removes only the optional avatar SQL from
the blocking auth bootstrap while keeping authoritative identity, roles and
credits on the SSR path.
