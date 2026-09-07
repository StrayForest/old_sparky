# Archived authenticated HTML/TTFB admission candidate — 2026-09-07

Status: rejected after exact production A/B. Owner: Platform maintainers.

This candidate followed the fresh attribution in
[`performance-authenticated-html-ttfb-2026-09-07.md`](performance-authenticated-html-ttfb-2026-09-07.md).
It changed only the reviewed authenticated-read admission profile:
`24` in-flight reads per API worker, up to `8` connection-free waiters and a
`250 ms` waiter timeout. API workers, DB pool size/overflow, pool pre-ping,
connection budget, Ready Vote admission and retry policy were unchanged.

The exact candidate deployment was run
[`34137257393`](https://github.com/StrayForest/old_sparky/actions/runs/34137257393)
from source SHA `ce936feb63e3a56cae82f2cc93ac4f7fbfd4842f`, followed by the
unchanged `authenticated-page-load-v1` profile
[`34137667234`](https://github.com/StrayForest/old_sparky/actions/runs/34137667234).
All 20,000 responses were HTTP 200 with zero errors, retries, overload
responses and exact cleanup. The stress behavior gate passed, but the target
did not:

| Metric | Candidate |
| --- | ---: |
| HTML TTFB p95 / p99 | `2356.822 / 3272.731 ms` |
| Full page p95 / p99 | `3081.330 / 4414.779 ms` |
| `/auth/bootstrap` p95 | `917.773 ms` |
| Workspace p95 | `1042.102 ms` |
| CPU per core | approximately `96.53% / 96.14%` |
| PostgreSQL backend peak / waiting backends | `51 / 3` |

Relative to the retained `2568.859 ms` TTFB p95, this was an approximately
`8.3%` improvement, far short of `<1000 ms`. Production was restored to
`authenticated-read-admission-32` by successful deployment
[`34139918905`](https://github.com/StrayForest/old_sparky/actions/runs/34139918905).

The candidate is rejected. The remaining evidence points to the web request
waterfall: root layout auth is awaited before the tournament page can start
its workspace fetch, while both API paths remain CPU-pressured. No worker or
pool scaling is inferred from this result.
