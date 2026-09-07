# Production performance stage — 2026-09-07

Status: completed with an explicit authenticated-HTML follow-up. This is the
canonical closeout for the measurement-correction, D9 candidate and full
production matrix work order in
[`performance-stage-request-2026-09-07.md`](performance-stage-request-2026-09-07.md).

## Scope and release

- All measurements below ran against deployed source SHA
  `bba3fb278e348906a6942aee8462b758c3d616ef`.
- The original 13 dispatchable production profiles were rerun with their
  existing contracts, thresholds and fixture sizes.
- Lifecycle profiles were not run against production.
- The measured client ran on a GitHub-hosted external runner. Production only
  prepared marked fixtures, observed bounded origin pressure and performed
  exact cleanup.
- Every run passed its profile acceptance and exact cleanup. No fixture users,
  tournaments, sessions or audit rows remained after cleanup.

## Matrix result

`p95/p99` and `TTFB p95/p99` are milliseconds. The `200/503` split is shown
for stress profiles where `503` is the declared controlled-overload response;
it is not an unexpected-status failure.

| Profile | Run | Fixture | Requests / statuses | p95 / p99 | TTFB p95 / p99 | Backend max | Decision |
| --- | ---: | --- | --- | ---: | ---: | ---: | --- |
| `authenticated-page-load-v1` | [34096416799](https://github.com/StrayForest/old_sparky/actions/runs/34096416799) | 20k users / 40 tournaments | 20,000 / 20,000 | 3357.939 / 4286.840 | 2568.859 / 3419.686 | 51 | stress behavior pass |
| `ready-vote-slo-v2` | [34098776746](https://github.com/StrayForest/old_sparky/actions/runs/34098776746) | 500 / 1 | 601 / 601×200 | 409.396 / 432.471 | 409.266 / 432.355 | 51 | SLO pass |
| `ready-vote-capacity-ramp-v2` | [34098971526](https://github.com/StrayForest/old_sparky/actions/runs/34098971526) | 20k / 40 | 10,521 / 10,521×200 | 173.955 / 229.677 | 173.759 / 229.568 | 51 | capacity complete |
| `ready-vote-saturation-ramp-v1` | [34100579366](https://github.com/StrayForest/old_sparky/actions/runs/34100579366) | 20k / 40 | 16,558 / 14,873×200 + 1,685×503 | 528.313 / 732.121 | 528.221 / 732.027 | 51 | stress behavior pass |
| `ready-vote-saturation-ramp-v2` | [34102128764](https://github.com/StrayForest/old_sparky/actions/runs/34102128764) | 20k / 40 | 25,110 / 15,870×200 + 9,240×503 | 545.998 / 705.534 | 545.877 / 705.428 | 51 | stress behavior pass |
| `ready-vote-saturation-ramp-v3` | [34103704225](https://github.com/StrayForest/old_sparky/actions/runs/34103704225) | 20k / 40 | 16,558 / 13,103×200 + 3,455×503 | 589.871 / 760.078 | 589.718 / 759.948 | 51 | stress behavior pass |
| `ready-vote-saturation-ramp-v4` | [34105316474](https://github.com/StrayForest/old_sparky/actions/runs/34105316474) | 20k / 40 | 18,301 / 15,027×200 + 3,274×503 | 504.385 / 636.969 | 504.255 / 636.880 | 52 | stress behavior pass |
| `ready-vote-stress-15k-v2` | [34112595285](https://github.com/StrayForest/old_sparky/actions/runs/34112595285) | 15k / 30 | 27,142 / 13,974×200 + 13,168×503 | 571.553 / 711.178 | 571.453 / 711.047 | 51 | stress behavior pass |
| `ready-vote-stress-20k-v2` | [34113845670](https://github.com/StrayForest/old_sparky/actions/runs/34113845670) | 20k / 40 | 37,165 / 18,265×200 + 18,900×503 | 512.589 / 732.867 | 512.406 / 732.720 | 52 | stress behavior pass |
| `ready-vote-spike-v1` | [34115313086](https://github.com/StrayForest/old_sparky/actions/runs/34115313086) | 2k / 4 | 1,804 / 1,804×200 | 254.162 / 443.316 | 254.080 / 443.233 | 51 | spike behavior pass |
| `read-mix-human-v2` | [34106952727](https://github.com/StrayForest/old_sparky/actions/runs/34106952727) | 500 / 1 | 500 / 500×200 | 395.626 / 429.403 | 395.500 / 429.264 | n/a | SLO pass |
| `read-mix-stress-v2` | [34107185009](https://github.com/StrayForest/old_sparky/actions/runs/34107185009) | 20k / 40 | 30,000 / 20,000×200 + 10,000×304 | 1641.706 / 2046.474 | 1641.249 / 2045.926 | 52 | stress behavior pass |
| `read-mix-concurrency-ramp-v1` | [34108903886](https://github.com/StrayForest/old_sparky/actions/runs/34108903886) | 20k / 40 | 170,000 / 160,000×200 + 10,000×304 | 1188.714 / 1552.741 | 1187.529 / 1551.110 | 52 | stress behavior pass |

All profiles reported zero unexpected statuses. The Ready Vote stress and
saturation profiles shed only with their declared `503` overload response;
their stress gates also accepted bounded retries, latency and origin safety.
The read profiles had no errors, retries or overload responses. The
authenticated page profile returned 20,000/20,000 HTTP 200 responses with no
timeouts, 520s or 522s.

The concurrency ramp completed every c16–c128 stage with 20,000/20,000
successful requests per stage. Its aggregate result was p95/p99
`1188.714/1552.741 ms`; the profiler emitted no automatic knee
(`knee=null`) and no runtime concurrency setting was changed from this
diagnostic run.

## Authenticated HTML conclusion

The D9 shell projection was measured in the fresh exact-SHA control. It is
functionally clean and the workflow passed its stress contract, but the
authenticated HTML TTFB p95 was `2568.859 ms`, with total page p95
`3357.939 ms`. This run is not a same-window unchanged-code A/B and does not
prove a D9 improvement over the earlier D6 evidence (`TTFB p95 2776.080 ms`).
The requested authenticated TTFB target of `<1,000 ms` remains open.

The observed server contour was stable: the auth run recorded PostgreSQL
backend max `51`, average CPU pressure about `96.8%` per API core, Nginx HTML
request p95 `3219.1 ms`, upstream-connect p95 `1 ms`, upstream-header p95
`2228 ms`, bootstrap sampled p95 `1278.7 ms`, DB p95 `538.078 ms` and pool
checkout p95 `714.126 ms`. This narrows the remaining work to the
authenticated web/API path; it does not justify worker or pool scaling.

## Measurement and release corrections

- The safety gate now uses the `pg_stat_activity` backend count and ownership
  snapshot. `/proc/net/tcp*` established sockets remain a separate diagnostic
  and no longer masquerade as the PostgreSQL backend budget.
- Retained-load setup, observer, cleanup and abort use the exact immutable
  `/opt/oldsparky/platform/current` release and shared runtime. The production
  deploy state machine takes the retained-load lock, preventing a release
  change during a fixture or measurement transaction.
- The active `Protect dev` ruleset no longer requires pull-request approval.
  Branch deletion and non-fast-forward updates remain protected; exact-SHA
  security/build and the automatic production deployment chain remain required.

The next owner action is a bounded authenticated-HTML investigation and a
same-contract A/B only if it can isolate the remaining TTFB component. The
historical v3 timeout and anomaly `33991798604` remain unexplained and are not
assigned to Cloudflare without a correlated recurrence.
