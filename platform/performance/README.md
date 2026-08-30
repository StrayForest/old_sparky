# Platform performance contract

The JSON files in `profiles/` are the only authored canonical load contracts.
`tools/platform_load.py` validates and dispatches them; the existing
`tools/platform_external_load.py` remains the HTTP implementation detail.
`tools/platform_load_acceptance.py` owns the shared correctness and latency
evaluation.

## Canonical profiles

| Profile | Category | Scenario |
| --- | --- | --- |
| `ready-vote-human-v1` | load | 500-user human-shaped Ready Vote burst |
| `ready-vote-stress-v1` | stress | 20,000-user aggressive Ready Vote burst |
| `read-mix-human-v1` | load | 500-user human-shaped authenticated reads |
| `read-mix-stress-v1` | stress | 20,000-user authenticated reads and conditional reloads |

Each profile owns its versioned fixture shape, setup concurrency, offered
logical actions, HTTP concurrency, spread, timeout, retry policy, expected
statuses, correctness requirements, latency/failure budgets and exact cleanup
contour. Its SHA-256 digest is recorded with every retained result.

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
