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

Each profile owns its versioned fixture shape, setup concurrency, offered
logical actions, HTTP concurrency, spread, timeout, retry policy, expected
statuses, correctness requirements, latency/failure/resource budgets and exact
cleanup contour. Its SHA-256 digest is recorded with every retained result.

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

## Ready Vote evidence (2026-08-30/31)

The production reference was restored from baseline `e70d1e7869e36aa401f6dc9c7fd5b38fea20a597` to `ready-vote-static-8` (deploy run `33332517609`). The final measured runtime source was `e0d27295dc7990250dd0a37f0b2210ee15e5b111`; the later documentation-only release keeps the same runtime behavior. Static-8 is exact per worker: minimum/initial/maximum admission concurrency `8/8/8`, with two API workers. API pool size `24`, `max_overflow=0`, checkout timeout `10s`, Redis and the database/worker budgets were unchanged.

Canonical profile fingerprints used for the retained evidence are:

| Profile | Version | SHA-256 |
| --- | ---: | --- |
| `ready-vote-slo-v2` | 2 | `c13851df4526bb4e32ddd49b93cf2810cca2da42b19c569a2c2bc7843757543a` |
| `ready-vote-capacity-ramp-v2` | 2 | `f4956f9f0e282c44ce3adc72eeeb342cce650979336f737e032df47567ea533c` |
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

## Canonical commands

```bash
cd platform
.venv_platform/bin/python tools/platform_load.py list
.venv_platform/bin/python tools/platform_load.py validate
```

The production workflow accepts only a reviewed profile ID. An operator
override is a non-canonical experiment and must retain the changed values and
must not be compared directly with canonical evidence.
