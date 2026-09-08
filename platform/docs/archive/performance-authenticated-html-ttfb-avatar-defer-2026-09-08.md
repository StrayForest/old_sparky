# Archived authenticated HTML/TTFB candidate — deferred bootstrap avatar — 2026-09-08

Status: accepted as a safe incremental improvement, but not target-closing.
Owner: Platform maintainers.

The candidate kept the authoritative session query, active-user and role
checks, credits, SSR identity, admin visibility and the no-JavaScript
authenticated header. It removed only the optional avatar projection from the
blocking `/auth/bootstrap` response. The header's safe icon fallback remains
available, while profile surfaces retain their existing avatar reads. Ready
Vote, workspace authorization, security headers, API contracts, worker count,
pool limits and PostgreSQL budgets were unchanged.

The exact production A/B on source SHA
`72e4c5df7af10d3d69c13ca4df938fbdb201e778` was run as
[`34174264965`](https://github.com/StrayForest/old_sparky/actions/runs/34174264965):

| Population | Requests | p95 ms | p99 ms |
| --- | ---: | ---: | ---: |
| HTML page | 20,000 | 1,868.297 | 2,127.828 |
| HTML TTFB | 20,000 | 1,512.860 | 1,728.350 |

All `20,000/20,000` responses were HTTP 200, with zero errors, unexpected
statuses, retries, 520/522 responses and shedding. The exact cleanup removed
all `20,000` users and `40` tournaments; remaining users, tournaments,
sessions and audit rows were zero. Relative to the retained baseline TTFB p95
`2,568.859 ms`, this is a `1,055.999 ms` (`41.10%`) reduction, but it remains
`512.860 ms` above the `<1,000 ms` target.

## Fresh origin attribution

The same source was deployed with sampled `web-ssr-diagnostics` and measured
by [`34175851102`](https://github.com/StrayForest/old_sparky/actions/runs/34175851102).
The client runner reached an `IncompleteRead` on one chunked response and did
not produce a valid full-population client report, so that workflow correctly
failed its external acceptance gate. This is rejected as a client acceptance
result, not used as a TTFB A/B. Exact cleanup still passed: all `20,000` users
and `40` tournaments were removed with zero remnants.

The origin observer captured all `20,000` HTML Nginx records during the
diagnostic window:

- upstream connect p95: `1 ms`;
- upstream header p95: `1,119 ms`;
- total upstream/request p95: `1,552 ms`;
- sampled `auth_bootstrap_fetch` p95: `78.425 ms`;
- sampled `root_layout_auth_bootstrap` p95: `79.438 ms`;
- sampled `root_layout_component_tree` p95: `80.228 ms`;
- event-loop p95/max p95: `57.377/168.126 ms`;
- `deadlock-web` CPU: `95.79%` average, `128.21%` max;
- API CPU: `38.97%` average; PostgreSQL CPU: `6.03%` average;
- PostgreSQL backends peaked at `43`, lock waiters at `0`, ungranted locks at
  `0`; Gunicorn remained at two workers and listen overflow/drop deltas were
  zero.

This rejects another auth-query/avatar/SQL hypothesis as the primary remaining
tail. The evidence now points to CPU/queue pressure in the single Next.js web
process after Nginx and API/DB connection time are excluded. The next bounded
candidate is the active server/client chrome boundary split.

## Production recovery record

The diagnostic profile was restored to `ready-vote-static-8` on the exact SHA
by successful deploy
[`34177592468`](https://github.com/StrayForest/old_sparky/actions/runs/34177592468).
The first restore was correctly stopped by stale backup freshness, and the
next attempt was stopped by the release lock; a guarded recovery confirmed no
durable pending release receipt. A fresh restore-verified backup was created
by [`34177273416`](https://github.com/StrayForest/old_sparky/actions/runs/34177273416)
before the successful retry. No manual lock or service bypass was used.
