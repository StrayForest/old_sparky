# Performance checkpoint: 2026-09-03

- Status: Historical
- Owner: Platform performance
- Last reviewed: 2026-11-23

## Read-path candidate slice (2026-09-03)

The retained read-mix baseline remains the comparison point: source
`4be82f1a…`, workflow `33625164162`, raw p50/p95/p99 `1212/2382/3492 ms`,
99.42 requests/s, API CPU about 98% per core and zero errors. The following
read-path changes were then accepted on exact-SHA production evidence:

- request-performance now distinguishes checkout wait, connection hold,
  SQL time and total request time;
- workspace and `/users/me` use an early session release after model
  materialization, while the workspace hot preflight uses primitive column
  snapshots instead of ORM hydration;
- global SSR auth uses `/auth/bootstrap`; full `/users/me` remains for profile
  and settings/account-security surfaces;
- the external runner has `read-mix-concurrency-ramp-v1`, which records every
  c16–c128 full-population stage separately;
  `capacity_ramp` is auxiliary evidence rather than a user-visible phase, but
  its authored stage keys, order, populations and per-stage budgets are
  mandatory; missing, altered or unknown auxiliary stages fail closed;
- `uvloop`/`httptools`, Nginx upstream keepalive, pool sizes 12/16/20/24,
  `pool_pre_ping=false` and authenticated-read admission 32 were isolated
  runtime A/B profiles.

The accepted runtime keeps JSON encoding, media loading, the
security-sensitive Redis session-validity model, and the two-worker/API-pool
envelope unchanged. Production uses `authenticated-read-admission-32`, API
pool `24` with `max_overflow=0`, `pool_pre_ping=true`, and the PostgreSQL
safety budget remains `52`. The final profile was applied by exact-SHA deploy
[`33726577559`](https://github.com/StrayForest/old_sparky/actions/runs/33726577559).

### Profile cache and upstream keepalive candidate (measured 2026-09-03)

The current development candidate changes profile-read access to a GET-first
lookup on the process-local shared Redis pool, taking the single-flight lock
only after a miss. A profile-less user is represented by a versioned 60-second
negative sentinel, so repeated `/auth/bootstrap` requests do not repeat the
profile builder SQL; profile creation and mutation paths invalidate the entry
after commit. The Nginx HTML route also uses a named `platform_web` upstream
with HTTP/1.1 keepalive and the inherited cleared `Connection` header.

The candidate was deployed by production run `33755347161` from source
`19e1f246cb97fed13671bfb9e234e18904522a2b` and measured by the exact-cleanup
external run [`33760930891`](https://github.com/StrayForest/old_sparky/actions/runs/33760930891)
using `authenticated-page-load-v1` (version 1, digest
`fe29f8bf227c78e301d86d4775792bc21f02ac8f17fcc39d1a92dcfece966b57`) at c64:

| Population | Requests | p50 ms | p95 ms | p99 ms | Max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full HTML page | 20,000 | 2295 | 3795 | 4417 | 8092 |
| HTML TTFB | 20,000 | 1744 | 3250 | 3852 | 7613 |

All 20,000 responses were `200`, with zero errors, zero retries and zero
shedding; wall time was `731.458 s` at `27.343` requests/s. The origin observer
recorded `/bootstrap` at 15,480 requests with average SQL count `2.0`, server
request p95/p99 `2051/2415 ms`, and the normalized profile-builder statement
at 15,480 calls. This workload gives each fixture user one page load, so it
does not itself exercise repeated negative-cache hits. Origin PostgreSQL
connections peaked at `51` (`48 api + 2 worker + 1 observer`), lock waiters
peaked at `0`, and API process CPU averaged `87.22%`.

The same run also verified Nginx upstream reuse: all 20,000 marked HTML
requests went to `127.0.0.1:3000`; `connection_requests` had median `266` and
maximum `1000`. This confirms that the named `platform_web` keepalive is active
on the real HTML path.

An isolated loopback probe against the deployed API used a temporary active
session whose user had no `PlayerProfile`, then removed the user/session and
both Redis keys. Across 20 sequential `/auth/bootstrap` requests, the
negative-sentinel phase produced one profile SELECT (the first fill) and 20
auth SELECTs; forcing a profile miss before every request produced 20 profile
and 20 auth SELECTs. A positive-cache phase produced 20 Redis `GET`s with no
`SET` or `EVAL`. Therefore hypothesis 1 is confirmed for warm reads and
hypothesis 2 is confirmed for repeated absent-profile requests: the profile
SQL path changes from `N` to `1` over the sentinel TTL, while authoritative
session SQL remains per request.

Compared with the retained prior page A/B (`33744391709`), the candidate moved
full-page p95/p99 from `3901/4426 ms` to `3795/4417 ms` and HTML TTFB p95/p99
from `3423/3796 ms` to `3250/3852 ms`. This is directional evidence rather
than a clean same-window A/B; the causal cache conclusions above come from the
isolated `pg_stat_statements` and Redis-command probe. Hypotheses 3, 4, 6 and 7
remain unmeasured.

### Read-path ownership, ramp and SSR evidence (2026-09-03)

The exact-SHA concurrency ramp
[`33716010545`](https://github.com/StrayForest/old_sparky/actions/runs/33716010545)
used the full 20,000-user read mix. Its stage results were:

| HTTP concurrency | p95 ms | p99 ms | Throughput req/s |
| ---: | ---: | ---: | ---: |
| 16 | 288.658 | 374.756 | 80.552 |
| 32 | 529.440 | 685.602 | 104.298 |
| 48 | 1080.505 | 1333.289 | 95.630 |
| 64 | 1040.676 | 1264.266 | 101.799 |
| 80 | 1436.423 | 1722.248 | 99.403 |
| 96 | 1354.994 | 1687.727 | 103.961 |
| 112 | 1855.392 | 2253.597 | 95.488 |
| 128 | 1589.240 | 1925.639 | 108.532 |

The report identifies `32` as the stable knee and `48` as the first queued
stage: p95 rose materially while throughput did not. Admission limit 32 was
therefore selected by the operator and validated in the authenticated-page
A/B; the ramp never changes runtime limits automatically.

The real Next.js HTML benchmark
[`33719680133`](https://github.com/StrayForest/old_sparky/actions/runs/33719680133)
at c64 had no errors or bad statuses, but exposed uncontrolled origin queueing:
full-page p95/p99 `10378/10714 ms`, HTML TTFB p95 `3062 ms`, and pool checkout
p95 `10001 ms`. With `authenticated-read-admission-32`,
[`33722487079`](https://github.com/StrayForest/old_sparky/actions/runs/33722487079)
passed the stress-behavior gate with no errors or shedding: full-page p95/p99
`3906/4425 ms`, HTML TTFB p95 `3440 ms`, pool checkout p95 `533 ms`, and
goodput `25.497` versus `17.83` requests/s. This is a diagnostic stress
comparison, not a claim that the public production SLO is the same as the
20,000-request benchmark.

The PostgreSQL observer attributed the peak of `51` established connections
to API `48`, worker `2` and observer `1`; one transient sample was unknown.
This is within the `52` safety budget, so the API pool was not reduced or
expanded. The observer also records normalized `pg_stat_statements` rows and
excludes only its own diagnostic queries. Exact cleanup after the ramp
([`33719571565`](https://github.com/StrayForest/old_sparky/actions/runs/33719571565)),
page baseline, page A/B and pre-ping A/B removed all fixture users,
tournaments, sessions and audit rows; the designated control account was
preserved.

The isolated `pool_pre_ping=false` stress A/B
[`33725036233`](https://github.com/StrayForest/old_sparky/actions/runs/33725036233)
passed functional stress checks but was not selected: p95/p99 were
`1741/2177 ms` at `105.367` requests/s, worse than the retained accepted
comparison, while stale-connection protection was removed. Production was
restored to `pool_pre_ping=true` in the final deploy above.

Public/static HTML remains deliberately deferred. The nonce CSP, dynamic
authenticated layout and `private, no-store` HTML policy were not bypassed or
weakened.
