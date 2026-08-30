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

## Canonical commands

```bash
cd platform
.venv_platform/bin/python tools/platform_load.py list
.venv_platform/bin/python tools/platform_load.py validate
```

The production workflow accepts only a reviewed profile ID. An operator
override is a non-canonical experiment and must retain the changed values and
must not be compared directly with canonical evidence.
