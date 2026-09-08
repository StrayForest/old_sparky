# Archived authenticated HTML/TTFB candidate — narrower provider boundary — 2026-09-08

Status: rejected as a target-closing result; safely improved but remained above
the requested target. Owner: Platform maintainers.

## Objective and candidate

The objective was to reduce `authenticated-page-load-v1` HTML TTFB p95 from
`2568.859 ms` to `<1000 ms` while preserving the read path, Ready Vote,
authentication and security semantics, no-JavaScript authenticated header
behavior, and the unchanged production worker/pool/backend contour.

The candidate moved `SiteFooter` outside `AuthProvider` in the root layout.
`SiteHeader` and the page subtree remained inside the authoritative server
auth bootstrap and `AuthProvider`, so authenticated identity, role-gated
header actions, auth lifecycle updates, and no-JavaScript header rendering
were preserved. No API, SQL, worker, pool, admission, retry, permission or
security policy changed.

Local `web-quality` and `web-hermetic` passed. The candidate source was
reviewed and merged to `dev` as SHA
`6e3a6325a0c6bee025297a8c8edb00817faafb75`; its immutable production deploy
was [run 34190879781](https://github.com/StrayForest/old_sparky/actions/runs/34190879781)
with the unchanged `ready-vote-static-8` profile.

## Exact production measurement

The first attempt [run 34191220895](https://github.com/StrayForest/old_sparky/actions/runs/34191220895)
was not a latency result: it was canceled after the external client remained
in the load phase because another retained-load/cleanup operation held the
host lock. Its required abort and exact cleanup completed successfully.

After the lock cleared, the same exact profile completed in
[run 34192721178](https://github.com/StrayForest/old_sparky/actions/runs/34192721178):

| Measure | Result |
| --- | ---: |
| Fixture | 20,000 users / 40 tournaments |
| HTTP responses | 20,000 / 20,000 HTTP 200 |
| HTML TTFB p50 / p90 / p95 / p99 | `1023.675 / 1253.396 / 1341.322 / 1559.344 ms` |
| Total page p50 / p90 / p95 / p99 | `1280.070 / 1551.651 / 1665.096 / 1889.758 ms` |
| Errors / unexpected / overload / retries | `0 / 0 / 0 / 0` |
| PostgreSQL backends | `43` peak, within `52` |
| PostgreSQL lock waiters | `0` |
| API workers | `2` |
| Web CPU | `87.16% / 84.28%` average per core; `100%` maximum |
| Cleanup | `ok=true`; 20,000 users and 40 tournaments deleted; zero remnants |

Compared with the unchanged control p95 of `2568.859 ms`, the candidate
reduced HTML TTFB p95 by `1227.537 ms` (`47.8%`) and total page p95 by
`1692.843 ms` (`50.4%`). The candidate still missed the required `<1000 ms`
TTFB p95 by `341.322 ms`, so it is rejected as the solution and is not kept
in production.

The sampled origin evidence showed the remaining contour was still CPU and
queue sensitive rather than a database lock failure: web CPU averaged about
`97.08%`, PostgreSQL backends peaked at `43`, lock waiters were `0`, and the
sampled `/auth/bootstrap` request had pool-checkout p95 `535.779 ms` despite
auth SQL p95 `19.308 ms` and zero avatar-query time. These sampled quantiles
are attribution evidence and are not additive to the full client TTFB
population.

The candidate was reverted through the reviewed `dev` path and production was
restored to the safe `ready-vote-static-8` contour before another candidate is
attempted. The next candidate must preserve the server-rendered authenticated
header and use a fresh exact production A/B; no capacity-setting change is
authorized by this result.
