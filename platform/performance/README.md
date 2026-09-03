# Platform performance contract

The JSON files in `profiles/` are the only authored canonical load contracts
(schema 2). `tools/platform_load.py` validates, fingerprints and dispatches
them; `tools/platform_external_load.py` remains the external HTTP
implementation detail. `tools/platform_load_acceptance.py` owns the shared
SLO, capacity, spike and stress decisions.

The pre-separation v1 contracts remain under `profiles/retained-v1/` solely as
historical semantics for interpreting retained evidence. That directory is
outside the active registry and is not selectable by the production workflow.

## Canonical profiles

| Profile | Category | Scenario |
| --- | --- | --- |
| `ready-vote-slo-v2` | load/SLO | 500-user human-shaped Ready Vote supported-load contract |
| `ready-vote-capacity-ramp-v2` | capacity | 20–80 logical Ready Vote actions/s, 30s steady phases |
| `ready-vote-saturation-ramp-v1` | saturation stress | 80–120 logical Ready Vote actions/s, 30s steady phases |
| `ready-vote-saturation-ramp-v2` | saturation stress | 120–165 logical Ready Vote actions/s, 30s steady phases |
| `ready-vote-saturation-ramp-v3` | saturation stress | 105–120 logical Ready Vote actions/s, 30s steady phases |
| `ready-vote-saturation-ramp-v4` | saturation stress | 120–135 logical Ready Vote actions/s, 30s steady phases |
| `ready-vote-stress-15k-v2` | stress | 15,000-user aggressive Ready Vote behavior test |
| `ready-vote-stress-20k-v2` | stress | Optional 20,000-user unresolved-question stress test |
| `ready-vote-spike-v1` | spike | Normal → burst → normal with recovery phases |
| `read-mix-human-v2` | load | 500-user human-shaped authenticated reads |
| `read-mix-stress-v2` | stress | 20,000-user authenticated reads and conditional reloads |
| `read-mix-concurrency-ramp-v1` | stress diagnostic | Full read mix at c16/c32/c48/c64/c80/c96/c112/c128 |
| `tournament-lifecycle-slo-v1` | load/SLO | 20 tournaments × 500 users through completed bracket |
| `tournament-lifecycle-scale-v1` | stress | Concurrent multi-tournament lifecycle waves |
| `tournament-lifecycle-capacity-v1` | capacity | Lifecycle capacity contour using the QA harness |

Each profile owns its versioned fixture shape, setup concurrency, offered
logical actions, HTTP concurrency, spread, timeout, retry policy, expected
statuses, correctness requirements, latency/failure/resource budgets and exact
cleanup contour. Its SHA-256 digest is recorded with every retained result.

The tournament-lifecycle profiles are executed only by
`platform_production_qa.py` against the configured QA/preprod origin. They are
not dispatchable through the external production load client and do not invoke
the external 15k/20k workflows. The harness reports each lifecycle phase with
full HTTP request/success/error/percentile/throughput/goodput/response-byte
metrics plus the existing system sampler and diagnostic `request_perf` data.

The v2 SLO profile applies the supported-load contract: accepted request
p50/p90/p95/p99 of 250/400/600/1000 ms, logical p95/p99 of 600/1000 ms,
final logical failure below 0.5%, and effectively zero overload shedding. A
capacity ramp reports SLO capacity and maximum stable goodput separately.
Stress and spike profiles deliberately do not inherit the final-failure SLO;
they evaluate bounded latency, retry amplification, shedding, origin CPU/DB
pool/wait evidence, durable correctness and cleanup. A stress result may end
with many logical failures and still conclude `STRESS BEHAVIOR PASS`; that is
not a capacity-target pass.

The capacity runner allocates unique fixture users across rate phases and
paces each phase for its declared steady duration. The spike runner preserves
phase boundaries so pressure entry, peak latency, shedding, goodput, retry
amplification and recovery can be compared instead of hidden in one aggregate.

The measured HTTP generator runs only on the GitHub-hosted runner. The
production origin may prepare marked fixtures, collect lightweight pressure
evidence and perform exact cleanup; it must not execute the measured client.

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
- `uvloop`/`httptools`, Nginx upstream keepalive, pool sizes 12/16/20/24,
  `pool_pre_ping=false` and authenticated-read admission 32 were isolated
  runtime A/B profiles.

The accepted runtime keeps JSON encoding, media loading, the
security-sensitive Redis session-validity model, and the two-worker/API-pool
envelope unchanged. Production uses `authenticated-read-admission-32`, API
pool `24` with `max_overflow=0`, `pool_pre_ping=true`, and the PostgreSQL
safety budget remains `52`. The final profile was applied by exact-SHA deploy
[`33726577559`](https://github.com/StrayForest/old_sparky/actions/runs/33726577559).

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

## Ready Vote evidence (2026-08-30/31)

The production reference was restored from baseline `e70d1e7869e36aa401f6dc9c7fd5b38fea20a597` to `ready-vote-static-8` (deploy run `33332517609`). The final measured runtime source was `e0d27295dc7990250dd0a37f0b2210ee15e5b111`; the later documentation-only release keeps the same runtime behavior. Static-8 is exact per worker: minimum/initial/maximum admission concurrency `8/8/8`, with two API workers. API pool size `24`, `max_overflow=0`, checkout timeout `10s`, Redis and the database/worker budgets were unchanged.

Canonical profile fingerprints used for the retained evidence are:

| Profile | Version | SHA-256 |
| --- | ---: | --- |
| `ready-vote-slo-v2` | 2 | `c13851df4526bb4e32ddd49b93cf2810cca2da42b19c569a2c2bc7843757543a` |
| `ready-vote-capacity-ramp-v2` | 2 | `f4956f9f0e282c44ce3adc72eeeb342cce650979336f737e032df47567ea533c` |
| `ready-vote-saturation-ramp-v1` | 1 | `804c6c5f882fc41ceef6087706e3cd61db7b9c4c1c629773ad2fa32744a6451f` |
| `ready-vote-saturation-ramp-v2` | 2 | `47452144eb575bd6bee2184710b8d325e43499b921875b94400a7160877a0d54` |
| `ready-vote-saturation-ramp-v3` | 3 | `d34c2537469daa6be0fdefa065306a7b82db911aaf151ffaba8480fb08d65fd8` |
| `ready-vote-saturation-ramp-v4` | 4 | `be8a2da8bab1ab966acfc90863c646d011eae36fd19034bbf2df1c91e8622e17` |
| `ready-vote-stress-15k-v2` | 2 | `a9fb7897fd228a8314ee0e02bef5c11e9149045adaecddd13ee3cc4f022cc8c8` |
| `ready-vote-spike-v1` | 1 | `6351a06a342b6170bb9f7bb2a280bd4bbbdf34443b90dc5df39698a0a52c6895` |

The supported-load SLO run (`33335115575`, source `77fd8682`) issued 500
logical actions with no retries, shedding or final failures. Accepted latency
was p50/p90/p95/p99 `241.711/256.963/264.706/639.338 ms`; logical
p95/p99 was `264.831/639.447 ms`, and the maximum logical latency was
`720.010 ms`. CPU peaked at `50.46%/41.28%` on the two cores, PostgreSQL
connections peaked at `23`, and waiters/lock waiters were `0/0`. The SLO
passed. The first SLO run (`33334438643`) was rejected only because the
optional slow-request server sample was absent; client-population metrics and
cleanup were already valid. Missing server pool spans are diagnostic gaps, not
missing client measurements.

The controlled static-8 capacity ramp (`33335224474`, source `77fd8682`) used
30-second steady phases at 20, 30, 40, 50, 60, 70 and 80 logical actions/s.
The table reports actual offered rate, goodput, accepted p50/p90/p95/p99,
logical p95/p99, shedding, retry amplification and the phase decision:

| Target | Offered | Goodput | Accepted p50/p90/p95/p99 ms | Logical p95/p99 ms | Shed / retry | Decision |
| ---: | ---: | ---: | --- | --- | ---: | --- |
| 20 | 20.033 | 19.872 | 231.744/256.536/264.886/647.462 | 264.951/647.572 | 0% / 0% | PASS |
| 30 | 30.033 | 29.797 | 227.330/250.689/255.471/265.213 | 255.552/265.272 | 0% / 0% | PASS |
| 40 | 40.033 | 39.720 | 229.722/253.463/258.858/272.428 | 258.927/272.492 | 0% / 0% | PASS |
| 50 | 50.033 | 49.650 | 230.765/258.588/270.976/349.578 | 271.049/349.657 | 0% / 0% | PASS |
| 60 | 60.033 | 59.574 | 237.827/268.067/283.167/346.075 | 283.242/346.120 | 0% / 0% | PASS |
| 70 | 70.033 | 69.444 | 238.491/301.039/341.897/437.267 | 342.168/443.482 | 0.095% / 0.095% | FAIL |
| 80 | 80.033 | 79.385 | 252.452/351.609/381.969/482.303 | 390.269/710.378 | 0.990% / 1.000% | FAIL |

Therefore strict SLO capacity is `60 actions/s`; maximum observed stable
goodput is `79.385 actions/s` at the 80 phase, which is reported separately
because that phase violates the SLO. The knee/shedding onset is 70: it is the
first phase with 503s, shedding and retries. At 80, p50 also exceeds the SLO
and logical p99 rises to `710.378 ms`. CPU rose from `61.82%` to `99.17%`
per core across the ramp, while PostgreSQL connections stayed at `25`, waits
peaked at `2` and lock waiters stayed at `0`; CPU corroborates the knee but is
not the decision criterion.

The exact same ramp with the candidate `ready-vote-adaptive-v2` was run in
`33337425315` after deploy `33337237276` (source `094519c7`). Its strict SLO
capacity was also only 60 actions/s, and its maximum observed goodput was
lower, `73.528 actions/s`. It first shed at 70 (`1.083%`, retry
amplification `1.095%`), then at 80 reached `32.042%` shedding, `38.75%`
retry amplification and `5.708%` final logical failures; logical p95/p99 was
`1607.448/1756.531 ms`. The controller correctly reacted to sustained CPU
pressure by reducing the per-worker limit from 8 to 4, but the result was not
a material improvement. Production was restored to static-8 by deploy
`33337943520`; adaptive-v2 is retained as an evaluated candidate, not the
runtime profile.

The spike run (`33338160338`, source `094519c7`) passed with static-8. Normal
→ burst → normal offered rates were `10.033 → 80.066 → 10.033` actions/s;
accepted p99 was `431.439 → 238.336 → 181.006 ms`, with zero retries,
shedding and final failures. CPU peaked at `88.50%/90.18%`, PostgreSQL
connections at `23`, and waiters/lock waiters at `2/0`. The normal-after phase
returned below the normal SLO and cleanup passed.

The 15k stress run (`33339421320`, source `e0d27295`) passed its stress
contract after the duplicate phase was corrected to accept explicit overload
503s while requiring every successful duplicate to be `changed=false`. The
primary phase had 15,000 logical actions, 10,749 successes, 4,251 final
overload failures, `66.216%` temporary shedding and `112.113%` retry
amplification; overall accepted p50/p90/p95/p99 was
`332.872/561.139/650.456/802.414 ms`. This is stress evidence, not a normal
traffic final-failure SLO. All successful primary changes were durable, all
successful duplicates were no-ops, unexpected statuses were zero, CPU peaked
at `100%/100%`, PostgreSQL connections at `24`, waiters/lock waiters at
`3/2`, and exact cleanup passed. The optional 20k stress profile was not run.

Every measured run completed the exact cleanup matrix: all fixture users,
tournaments, sessions and audit rows were removed, while the control account
was preserved. The capacity and stress runs included server pool samples;
SLO/spike runs had no selected server pool timing spans, so those spans remain
diagnostic-only and do not replace the complete client-side population
metrics. The retry policy remains bounded at two overload retries; no retry
increase or blind retry was introduced.

## Ready Vote saturation after the final FastAPI fast path (2026-08-31)

This is the controlled follow-up to baseline SHA
`6580f7bf5c02641a8ff607c35bcc050e24b1a50e`. Both baseline and candidate used
two API workers, two vCPUs, `ready-vote-static-8`, admission `8/8/8`, the same
PostgreSQL/Redis/DB-pool budgets and the same GitHub-hosted external runner.
The 15k stress result is retained as stress evidence only; it is not the
canonical saturation ceiling.

The baseline rate sweep was split across `ready-vote-saturation-ramp-v1`
(`33368575458`) and the refinement `ready-vote-saturation-ramp-v4`
(`33374294139`). The table is the phase evidence used for the envelope:

| Offered target | Actual offered | Goodput | Accepted p95/p99 ms | Logical p95/p99 ms | Shed / retry / final fail | Source |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 80 | 80.033 | 79.392 | 355.358/528.541 | 433.702/1061.840 | 3.583% / 3.500% / 0.208% | v1 |
| 90 | 90.033 | 89.555 | 255.418/324.256 | 256.458/327.976 | 0.148% / 0.148% / 0% | v1 |
| 100 | 100.033 | 99.469 | 286.216/360.248 | 302.499/588.383 | 1.153% / 1.167% / 0% | v1 |
| 110 | 110.033 | 106.289 | 335.207/417.300 | 707.241/1332.903 | 7.034% / 7.273% / 0.273% | v1 |
| 120 | 120.033 | 115.607 | 339.128/413.136 | 779.391/1547.376 | 11.371% / 11.639% / 1.056% | v1 |
| 125 | 125.033 | 116.327 | 475.436/583.190 | 1800.244/2003.147 | 28.569% / 34.880% / 3.653% | v4 |
| 130 | 130.033 | 114.469 | 490.933/610.145 | 1822.401/2019.541 | 30.587% / 35.974% / 5.615% | v4 |
| 135 | 135.032 | 102.569 | 559.898/651.012 | 1937.233/2083.696 | 55.224% / 83.852% / 17.679% | v4 |

The resulting envelope is: `HEALTHY <=70/s`; `SLO EDGE = 80/s`;
`CAPACITY KNEE = 90–110/s`; `SATURATION = 120–130/s`; and `OVERLOAD =
135/s`. Goodput stops materially increasing in the 120–130 band. The
canonical baseline maximum stable goodput is therefore reported as
`~116 actions/s`, not as the 15k stress goodput `94.409 actions/s` and not as
the noisy single-phase maximum. At the saturation band CPU reached
`~88–89%` per core average (100% maximum), API process CPU was about
`115%` aggregate, PostgreSQL about `29%` average, connections `42` maximum,
waiting backends `2`, lock waiters `1`, and peak observed query wait was
`188.73 ms`. This is CPU/backpressure saturation with bounded database
pressure, not a PostgreSQL pool ceiling.

The candidate A/B runs were `33379397589` (`capacity-ramp-v2`, 20–80/s),
`33381896491` (`saturation-ramp-v1`, 80–120/s), `33384057053` and
`33384821610` (`saturation-ramp-v4`, 120–135/s), and `33385667381`
(`saturation-ramp-v2`, 120–165/s), all from source SHA
`68eb3f421049f8135bdf3b72c723dc4d93c8f57f`. Candidate goodput was
`69.406/79.418` at 70/80/s and `89.507/99.385/109.336/119.120` at
90/100/110/120/s. The repeated v4 runs produced respectively
`114.887/115.406/126.900/118.158` and
`116.476/121.775/127.570/129.097` at 120/125/130/135/s. The v2 sweep
provided the conservative repeatable plateau: `114.535/117.243/114.502/109.275`
at 120/135/150/165/s. The isolated 126–129/s v4 observations are retained as
run evidence but are not called a stable ceiling because the two v4 runs and
the v2 contour do not reproduce that exact peak.

At 120/s candidate accepted p95/p99 was
`327.020/409.036 ms`, logical p95/p99 `431.467/815.235 ms`, shedding
`5.045%`, retries `5.167%`, final failure `0.139%`; CPU averaged
`75.10%/75.75%` per core, API process CPU `93.27%`, PostgreSQL `29.00%`,
connections `41`, waiting backends `1`, and lock waiters `0`. The improvement
is material at the high end but does not move the normal SLO contract: the
candidate SLO capacity remains `70/s`, and the knee remains approximately
`80/s`. For the final envelope, maximum stable candidate goodput is reported
as `~117 actions/s` from the v2 contour; the `119.120/s` v1 and `126–129/s`
v4 observations are not used to claim a larger stable ceiling. This is a
small throughput change within the broad run-to-run spread, while the CPU
compiler cost reduction is directly confirmed by cProfile.

The bounded before profile was `33376398557` on cprofile runtime, with
profiling overhead excluded from latency conclusions. The ranked CPU evidence
was:

1. SQLAlchemy PostgreSQL insert compilation/coercion: `coercions.expect`
   self CPU `3.742045 s`; the dialect `visit_insert` path used `0.481978 s`
   across `7,232` upsert calls (`~0.067 ms` self CPU per upsert).
2. Async/runtime transport and middleware: h11/socket/asyncio self CPU was
   the largest remaining wall-clock group under cProfile, but it is mostly
   I/O/scheduling and was not treated as an endpoint optimization target.
3. SQLAlchemy greenlet/session execution: `greenlet_spawn` self CPU
   `0.719933 s`; `AsyncSession.scalar` and the endpoint service were smaller
   than the statement compiler target. Response instrumentation measured
   response handling at about `0.008 ms`.

The only code change was a Ready Vote-specific cacheable SQLAlchemy
`TextClause` for the conditional upsert. It keeps `AsyncSession.scalar`, one
DB checkout, one transaction boundary, auth/preflight, conditional upsert,
commit/rollback, trigger-authoritative counters, and post-commit cache
invalidation unchanged. The regression test asserts that the shared upsert
statement has a cache key while retaining the `ON CONFLICT`, conditional
choice predicate and `RETURNING` contract.

The candidate after-profile was `33380666490`. `visit_insert` was absent from
both worker profiles; `upsert_deadlock_ready_vote` self CPU was only
`0.068245 s` aggregate in that diagnostic run. The remaining
`coercions.expect` self CPU was `2.489327 s`, attributable to the other
statement shapes (especially preflight/auth), not the removed dialect insert
compiler. The next bottleneck is therefore the shared request/middleware/
transport and preflight/session path, with h11 header normalization,
asyncio/HTTP scheduling, auth and preflight execution visible above the
endpoint-specific upsert. No primitive Core rewrite was justified: the
observed DB CPU, connection count and lock/pool pressure were bounded, and
the response path was negligible.

The first candidate `ready-vote-stress-15k-v2` run (`33382724584`) was not
called PASS: it had 17 external-runner status-0 network errors. Its server
statuses remained 200/503 and cleanup passed. The identical rerun
`33383445876` passed the stress contract: primary goodput `108.032/s`,
shedding `61.575%`, retry amplification `99.140%`, final logical failure
`23.480%`, accepted p95/p99 `409.872/552.084 ms`, CPU `93.03%/93.29%` per
core, PostgreSQL `26.43%`, connections `41`, waiting backends/lock waiters
`1/1`, and exact cleanup. This is controlled stress evidence, not a normal
traffic failure-rate target. Both stress runs removed all fixture rows and
preserved the control account.

## Read-mix stress evidence (2026-09-01)

The first valid production `read-mix-stress-v2` run after the fixture-name
collision fix was [workflow `33559300059`](https://github.com/StrayForest/old_sparky/actions/runs/33559300059),
source SHA `9c46f70f1d23aa84c4e1f66bdfdb9e617e767b61`. It used the canonical
fixture of 40 tournaments × 500 users: 20,000 authenticated virtual users,
128 HTTP workers and 10,000 conditional workspace refreshes. The external
runner issued 30,000 requests in 499.717 seconds:

- initial read mix: 20,000 requests — 10,000 workspace, 6,000 tournament
  detail, 2,000 `/users/me`, and 2,000 tournament-list reads;
- manual refresh: 10,000 conditional workspace requests — 9,998 `304`
  responses;
- overall raw HTTP p50/p95/p99: `2057/4674/6324 ms`;
- response statuses: `19,997 × 200`, `9,998 × 304`, `3 × 503`, and `2`
  missing-initial-ETag refreshes;
- retries and classified overload shedding: `0` and `0%`.

The stress decision was **`STRESS BEHAVIOR FAIL`**. The three `503` responses
were unexpected for this profile (one `/users/me`, two workspace reads), and
the two affected users could not perform their conditional refresh. Accepted
latency budgets and origin-safety checks passed, but the correctness and
unexpected-status checks did not.

Origin evidence identifies CPU/backpressure as the current bottleneck: both
API cores averaged about `99%` and reached `100%`; PostgreSQL reached `51`
connections, `6` waiting backends and `0` lock waiters. Diagnostic
`request_perf` samples recorded overall p95/p99 `4803/6475 ms`, average DB
time `351 ms`, and pool checkout p95/p99 `3490/5182 ms`. The largest sampled
route groups were workspace (`16,957` requests, average DB time `423 ms`),
slug detail (`4,223`, `173 ms`) and `/me` (`1,489`, `304 ms`). This is bounded
database pressure with CPU saturation and pool queueing, not a lock-wait
incident.

Exact cleanup passed: all `20,000` fixture users and `40` tournaments were
deleted, with zero remaining fixture users, tournaments, sessions or audit
rows; the control account was preserved. Earlier runs `33553459937` and
`33553627531` were setup failures and are not performance evidence.

This profile validates authenticated page/API reads and conditional ETag
reloads. It does not validate the create → join → Ready Check → bracket
lifecycle and does not replace the separate browser/grid gate.

## Read-mix stress baseline (2026-09-02)

The next canonical `read-mix-stress-v2` run was [workflow
`33582068816`](https://github.com/StrayForest/old_sparky/actions/runs/33582068816),
source SHA `eb2ade0c82c193d82bd3720c19d0e9f168fc2719`. It used the same
40-tournament × 500-user fixture: 20,000 authenticated users, 128 external
HTTP workers and 10,000 conditional workspace refreshes. The runner issued
30,000 requests in 784.547 seconds:

- raw HTTP p50/p95/p99: `3086/7112/10010 ms`;
- statuses: `19,954 × 200`, `9,943 × 304`, `79 × 503` and `24` transport/status-0 errors;
- API cores averaged `99.54%` and `99.30%`; PostgreSQL averaged `13.25%` CPU;
- PostgreSQL reached `55` connections and `19` waiting backends, with zero lock waiters;
- server `request_perf` p95/p99 was `6993/9897 ms`, with pool checkout p95/p99
  `5431/8153 ms`;
- exact cleanup passed: all 20,000 users and 40 tournaments were removed,
  with no remaining sessions or audit rows and the control account preserved.

The decision was **`STRESS BEHAVIOR FAIL`** because the profile allows only
`200/304` responses. The evidence points to API CPU saturation and database
pool queueing, not PostgreSQL CPU or lock contention. Cloudflare edge caching
was separately verified on the public catalog path; this run therefore does
not justify disabling Cloudflare or replacing it with an origin-only cache.

## Read-mix A/B after authenticated-read optimizations (2026-09-02)

The first candidate after the baseline was [workflow
`33585907064`](https://github.com/StrayForest/old_sparky/actions/runs/33585907064),
source SHA `11a39ffd6d87f026425519eb39c83d6deda1dbf5`. It used the same
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes. The runner issued all 30,000 requests
in 473.178 seconds:

- raw HTTP p50/p95/p99: `1936/3982/5396 ms` (baseline: `3086/7112/10010 ms`);
- status counts: `19,999 × 200`, `10,000 × 304`, `1 × 503`, and no transport/status-0 errors;
- API cores averaged `97.42%` and `97.66%`; PostgreSQL averaged `19.05%` CPU;
- PostgreSQL reached `51` connections and `10` waiting backends, with zero lock waiters;
- server `request_perf` p95/p99 was `3697/5143 ms`, with pool checkout p95/p99
  `2687/4274 ms` (baseline: `5431/8153 ms`);
- exact cleanup passed: all 20,000 users and 40 tournaments were removed,
  with no remaining sessions or audit rows and the control account preserved.

The stress decision remains **`STRESS BEHAVIOR FAIL`** because one `503` is
still outside the profile's `200/304` contract. The A/B result is materially
better: wall time fell by 39.7%, p95 by 44.0%, p99 by 46.0%, and unexpected
responses fell from 103 to 1. The remaining limit is API CPU saturation and
pool queueing; Redis and PostgreSQL are not resource ceilings. The candidate
removed per-request Redis client creation, combined the active Ready Check
workspace read, avoided an unnecessary initial public-list DB checkout, and
collapsed the current-user identity reads. A subsequent candidate removes a
duplicate organizer-profile lookup when the main tournament query already
returned an explicit missing avatar asset; it still needs a new production A/B
run before being treated as an accepted optimization.

## Read-mix A/B after duplicate avatar lookup removal (2026-09-02)

The next canonical run was [workflow
`33588749169`](https://github.com/StrayForest/old_sparky/actions/runs/33588749169),
source SHA `7425e72ef90934ea6bb8a57bbffeb53936475f66`. It used the same
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes. All 30,000 responses satisfied the
profile contract and exact cleanup passed:

- raw HTTP p50/p95/p99: `1792/3685/5050 ms`; wall time `432.030 s` and
  `69.44 req/s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with no 503 or transport errors;
- API cores averaged `98.57%` and `98.54%`; PostgreSQL averaged `18.95%` CPU;
- PostgreSQL reached `51` connections and `7` waiting backends, with zero lock
  waiters;
- server `request_perf` p95/p99 was `3603/4865 ms`, SQL/request `5.753`, and
  pool checkout p95/p99 `2668/3987 ms`;
- all 20,000 users and 40 tournaments were removed, with no remaining
  sessions or audit rows and the control account preserved.

The stress decision was **`STRESS BEHAVIOR PASS`**. Compared with the prior
A/B, wall time improved by 8.7%, p95 by 7.5%, p99 by 6.4%, and workspace SQL
fell from `8.012` to `7.012` per request. The API remains CPU-bound at roughly
98.5%; PostgreSQL CPU, locks and Redis latency remain below their ceilings.
The next reviewed candidate added a narrow conditional-detail fast path, but it
was not accepted: the follow-up canonical run below was slower and returned
origin `503` responses. The candidate was rolled back, leaving this
`7425e72e` runtime as the accepted production baseline.

## Read-mix validation after conditional-detail fast path (2026-09-02)

The follow-up canonical run was [workflow
`33591512977`](https://github.com/StrayForest/old_sparky/actions/runs/33591512977),
source SHA `df277f0c0bad09d32e59f9b0b5ff0af5791bb002`. It used the same
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes. The exact cleanup and origin-safety
checks passed, but the stress contract did not:

- raw HTTP p50/p95/p99: `1973/4232/5893 ms`; wall time `480.806 s`;
- statuses: `19,995 × 200`, `9,997 × 304`, `5 × 503` and `3` derived missing-ETag
  refreshes;
- all five `503` responses occurred in the initial read phase and coincided
  with DB pool checkout reaching the 10-second timeout; the three missing ETags
  were downstream of those failed initial reads;
- API cores averaged `98.47%` and `98.35%`; PostgreSQL averaged `18.47%` CPU;
- PostgreSQL reached `51` connections and `4` waiting backends, with zero lock
  waiters; server pool checkout p95/p99 was `2988/4858 ms` and reached
  `10052 ms`;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

The decision was **`STRESS BEHAVIOR FAIL`**. Compared with the accepted prior
run, the result was slower and therefore does not establish a gain from the
conditional-detail fast path under this load. Cloudflare was separately
verified to serve the public catalog with `cf-cache-status: HIT`; it remains
enabled, with Redis as the origin read-model fallback. The next bounded
candidate samples successful slow-request diagnostic logs at `0.25` in
production; 5xx diagnostics remain fully logged. This changes observability
overhead only and does not change authorization, ETag inputs or response
business rules.

## Read-mix A/B after diagnostic-log sampling (2026-09-02)

The sampling candidate was validated by [workflow
`33594516140`](https://github.com/StrayForest/old_sparky/actions/runs/33594516140),
source SHA `3a8363f0c8874757eb56c9b9db5595e0b4043fbd`. It used the canonical
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes. The profile contract and exact cleanup
both passed:

- raw HTTP p50/p95/p99: `1890/3752/5324 ms`; wall time `454.799 s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with zero errors and unexpected
  statuses;
- API cores averaged `98.72%` and `98.79%`, each reaching `100%`; PostgreSQL
  averaged `18.97%` CPU and reached `51` connections;
- PostgreSQL waiting backends peaked at `6`, lock waiters and ungranted locks
  stayed at `0`; server pool checkout p95/p99 was `2685/4147 ms`, with no
  checkout timeout or `503`;
- successful diagnostic records fell from `27,472` in the prior run to `6,685`
  under the `0.25` sample rate; 5xx logging remains unconditional;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

Sampling removed diagnostic-log volume but did not produce a reproducible
latency gain versus the accepted `33588749169` run: wall time and p95/p99 were
slightly higher in this sample. It was rolled back together with the
conditional-detail fast path. The durable conclusion is that the two-vCPU
API is CPU-bound: PostgreSQL CPU, Redis latency and lock contention remain
below their ceilings, while API CPU stays near 100% and pool wait is the
resulting queue. No worker/pool increase or cache rewrite is justified without
new profiling evidence; Cloudflare edge caching remains enabled and the Redis
read-model cache remains the origin fallback.

## Read-mix validation after rollback (historical checkpoint, 2026-09-02)

This was the post-rollback checkpoint before the next workspace read-path
candidate. The deployed source was
`ffe74e8c932b9f1fbe0c3c25d8cf1fd5207058c6`, whose effective runtime was the
`7425e72ef90934ea6bb8a57bbffeb53936475f66` implementation plus rollback
commits. It retained the authenticated tournament read-path optimizations and
duplicate organizer-avatar lookup removal; the conditional-detail fast path
and successful-log sampling were not present.

Post-rollback [workflow `33602219984`](https://github.com/StrayForest/old_sparky/actions/runs/33602219984)
ran the canonical 20,000-user, 40-tournament × 500-user fixture with 128
external workers and 10,000 conditional refreshes. It passed the stress
contract and exact cleanup:

- raw HTTP p50/p95/p99: `1967/4015/5363 ms`; wall time `444.082 s` and
  `67.555 req/s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with zero errors and unexpected
  statuses;
- API cores averaged `98.43%` and `98.37%`, each reaching `100%`; PostgreSQL
  averaged `18.93%` CPU and reached `52` connections;
- PostgreSQL waiting backends peaked at `6`, lock waiters and ungranted locks
  stayed at `0`; server `request_perf` p95/p99 was `4017/5370 ms`;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

The result was **`STRESS BEHAVIOR PASS`**. The historical run
`33588749169` remains the fastest measured sample for that runtime; the fresh
run was slower by `2.8%` wall time, but had the same contract and resource-
safety outcome.

## Read-mix A/B after combined workspace base/access preflight (2026-09-02)

The next candidate was deployed through [production deploy
`33618864270`](https://github.com/StrayForest/old_sparky/actions/runs/33618864270),
source SHA `227a076bac3a3cfd42e8c62901ce2a29928ddd52`. It combines the
tournament base row and authenticated viewer access/commitment lookup into one
cached SQL statement for requests without an invite code, while retaining the
revision-based conditional 304 preflight from `829ccc2b`. The canonical
20,000-user, 40-tournament × 500-user fixture, 128 external HTTP workers and
10,000 conditional workspace refreshes were measured by [workflow
`33619193304`](https://github.com/StrayForest/old_sparky/actions/runs/33619193304).
The profile contract, origin-safety checks and exact cleanup all passed:

- raw HTTP p50/p95/p99: `1341/2908/4414 ms`; wall time `353.045 s` and
  `84.975 req/s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with zero errors, retries,
  shedding or unexpected statuses;
- API cores averaged `98.36%` and `98.34%`; PostgreSQL averaged `21.49%`
  CPU and reached `41.28%` at peak;
- PostgreSQL waiting backends peaked at `9`, while lock waiters and ungranted
  locks stayed at `0`; workspace server pool checkout p95/p99 was
  `2068/3458 ms`;
- workspace request diagnostics fell from `6.018` to `4.501` SQL/request and
  from `438.892` to `278.235 ms` average DB time versus baseline run
  `33609896507`; the workspace pool checkout p95 fell from `3557` to `2068 ms`;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

The result was **`STRESS BEHAVIOR PASS`**. Wall time improved by `26.1%`
against the prior baseline and by `15.2%` against the immediately preceding
conditional-304 candidate (`33614703285`). The initial read phase improved
from `1849 ms` to `1576 ms` average versus the baseline; conditional refresh
latency remained within the same range (`1342 ms` in this run). This remains
the previous accepted workspace baseline for the next A/B below. The large
gain was consistent with removing one database checkout from authenticated
initial workspace reads; API CPU remained the limiting resource, while
PostgreSQL and Redis stayed below their resource ceilings.

## Read-mix A/B after combined workspace base/access/Ready Check preflight (2026-09-02)

The next candidate was deployed through [production deploy
`33624720919`](https://github.com/StrayForest/old_sparky/actions/runs/33624720919),
source SHA `4be82f1a9e682fda8bee990667b962f1f46e0b58`. It extends the cached
workspace base/access preflight with the selected Ready Check round, its
counter-shard common counts and the requesting user's vote overlay. The
workspace response contract is unchanged; the direct Ready Check endpoint,
vote mutation path and workflow writers remain authoritative and unchanged.
There is no schema or migration change. The canonical 20,000-user,
40-tournament × 500-user fixture, 128 external HTTP workers and 10,000
conditional workspace refreshes were measured by [workflow
`33625164162`](https://github.com/StrayForest/old_sparky/actions/runs/33625164162).
The profile contract, origin-safety checks and exact cleanup all passed:

- raw HTTP p50/p95/p99: `1212/2382/3492 ms`; wall time `301.757 s` and
  `99.418 req/s`;
- statuses: `20,000 × 200` and `10,000 × 304`, with zero errors, retries,
  shedding or unexpected statuses;
- API cores averaged `98.40%` and `98.58%`; PostgreSQL averaged `23.21%`
  CPU and reached `46.76%` at peak;
- PostgreSQL waiting backends peaked at `4`, while lock waiters and ungranted
  locks stayed at `0`; workspace server pool checkout p95/p99 was
  `1659/2656 ms`;
- workspace request diagnostics fell from `4.501` to `4.018` SQL/request and
  from `278.235` to `228.691 ms` average DB time versus the previous accepted
  baseline; workspace pool checkout p95 fell from `2068` to `1659 ms`;
- exact cleanup removed all 20,000 users and 40 tournaments, with no remaining
  sessions or audit rows and the control account preserved.

The result is **`STRESS BEHAVIOR PASS`**. Wall time improved by `14.5%`
against the previous accepted workspace baseline (`353.045 s` → `301.757 s`),
and throughput increased by `17.0%` (`84.975` → `99.418 req/s`). The initial
read phase in this run had average/p50/p95/p99 `1319/1253/2731/3720 ms`,
versus `1576/1364/3620/4668 ms` in the preceding candidate. The conditional
refresh sample was also faster (`1208 ms` average), but this candidate does
not claim that change because matching `304` requests use the existing
conditional preflight. The workspace Ready Check stage no longer performs a
separate read; its state is materialized from the combined preflight. API CPU
remains the limiting resource, while PostgreSQL and Redis remain below their
resource ceilings. Production now retains this combined
base/access/Ready-Check preflight plus the proven conditional 304 path.

## Rejected conditional workspace auth preflight (2026-09-02)

The next candidate, source SHA `6344168a6ca93d6d0665c7de9ada1fdd09efabf8`,
deferred the cached-session validation query for the exact conditional
workspace request shape and folded that validation into the existing
conditional workspace preflight. Authorization, session expiry/invalidation,
email-verification policy, ETag inputs and non-workspace routes were kept
unchanged. The candidate was deployed by [production deploy
`33647303565`](https://github.com/StrayForest/old_sparky/actions/runs/33647303565)
and measured twice against the canonical `read-mix-stress-v2` profile.

- [first run `33647757275`](https://github.com/StrayForest/old_sparky/actions/runs/33647757275): `STRESS BEHAVIOR FAIL`, with `296` client
  failures including one `522`; workspace diagnostics were `3.390`
  SQL/request and `222.722 ms` average DB time;
- [repeat `33650029282`](https://github.com/StrayForest/old_sparky/actions/runs/33650029282): `STRESS BEHAVIOR FAIL`, with `29,999/30,000`
  successful responses and one `URLError` (`status=0`); workspace
  diagnostics were `3.504` SQL/request and `203.693 ms` average DB time,
  while origin safety rejected PostgreSQL connections peaking at `55` against
  the `52` ceiling;
- both runs completed exact cleanup, but neither satisfied the full contract.

The experiment was therefore reverted by `38c06fda`; the known-good combined
base/access/Ready-Check preflight remains the production baseline. The lower
SQL and pool measurements are useful evidence for a future auth optimization,
but this implementation is not accepted without a contract-passing design.

## Canonical commands

## Tournament lifecycle read models

The lifecycle QA mode uses the existing API flow and stores only serialized,
revisioned tournament representations in the shared Redis instance:

- `teams` — normalized `detail.teams` state;
- `workspace_detail` — the normalized team projection embedded in detail
  workspace responses;
- `bracket_summary` and `bracket_full` — the existing bracket response shapes.

PostgreSQL remains authoritative. Successful team/bracket mutations rebuild
only the affected projections after commit. A Redis hit is served without a
representation-building PostgreSQL query; a miss or Redis outage builds from
PostgreSQL and returns the normal response. Redis writes use a revision-aware
atomic compare-and-set and a long safety TTL; TTL is not a consistency
mechanism. `request_perf` and the existing QA HTTP recorder expose hit/miss,
build/write/error/fallback, revision, build/get/set timing and payload bytes.

The lifecycle harness reports `teammate_profile_reads` and
`opponent_profile_reads` separately, uses the real detail/bracket workspace
queries, and checks both `200` and conditional `304` bracket refreshes.

```bash
cd platform
.venv_platform/bin/python tools/platform_load.py list
.venv_platform/bin/python tools/platform_load.py validate
```

The production workflow accepts only a reviewed profile ID. An operator
override is a non-canonical experiment and must retain the changed values and
must not be compared directly with canonical evidence.
