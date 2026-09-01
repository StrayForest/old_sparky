# Platform test-suite governance

- Status: Active reference
- Owner: Platform maintainers
- Last reviewed: 2026-09-01

The executable registry at `platform/tools/platform_verify.py` is the single
source of truth for verification ownership, commands, environment
requirements and CI membership. Use `list --json` when tooling needs the
machine-readable registry. This document explains the architecture and
placement rules; it does not repeat tool arguments.

## Verification layers

| Gate ID | Layer and owner | Environment | Normal trigger |
| --- | --- | --- | --- |
| `backend` | unit/integration, backend and domain owners | hermetic test PostgreSQL/Redis | local feedback + CI |
| `python-quality` | Python quality, backend/tooling owners | pinned quality dependencies | local feedback + CI |
| `security` | dependency and repository security owners | pinned platform/quality dependencies | local feedback + CI |
| `migration` | persistence owners | disposable PostgreSQL only | CI |
| `docs` | platform maintainers | repository checkout; Markdown docs and project skill metadata | local feedback + CI |
| `web-quality` | web owners | Node 26.3.1 and locked dependencies | local feedback + CI |
| `web-hermetic` | web owners | local/mocked API and Chromium | local feedback + CI |
| `verification-contract` | platform tooling owners | repository checkout | CI |
| `server-smoke` | release owners | exact deployed SHA | deployment workflow |
| `live-public` | production operators | canonical public origin and dedicated QA identity | explicit/release workflow |
| `live-user-destructive` | production operators | marked production fixtures and mandatory cleanup | explicit operator workflow |
| `external-load` | performance operators | external generator to production origin | explicit operator workflow |

The first eight gates are deterministic. `platform_verify.py ci` can execute
only those gates and never connects to production, creates production
fixtures, opens a production browser or starts a load generator. The latter
four remain discoverable governance groups but are workflow-only.

## Ownership rules

Use the lowest suitable layer in the test pyramid:

1. Put domain and API behavior in the auto-discovered `platform/tests/test_*.py`
   tree. A new ordinary backend test requires no workflow or filename-list
   edit.
2. Put important cross-component browser journeys in the existing hermetic
   Playwright suites under `apps/platform_web/tests`. The package’s hermetic
   script owns suite discovery; a new ordinary scenario requires no workflow
   edit.
3. Add a new deterministic contour to the executable registry, its runner,
   the verification-contract self-test and the CI job that invokes the gate.
4. Keep deployment smoke and real-origin QA in their protected production
   workflows. They are not substitutes for hermetic tests and do not expand
   into a production regression suite.
5. Add load, stress, spike, soak or capacity scenarios as versioned profiles
   under `platform/performance/profiles/`. The profile
   owns the complete scenario and acceptance contract. The same external-load
   workflow should orchestrate a new reviewed profile.

Never add an individual ordinary test by editing GitHub workflow YAML. Do not
hide deterministic failures with grep exclusions or silent retries. A flaky
test is explicit test debt with an owner, not a reason to weaken a gate.

## Local and GitHub verification

Local canonical gates provide fast developer feedback. They must use the
registry, for example:

```bash
cd platform
.venv_platform/bin/python tools/platform_verify.py list
.venv_platform/bin/python tools/platform_verify.py backend --focused tests.test_platform_domain
.venv_platform/bin/python tools/platform_verify.py python-quality
```

`LOCAL GATE BLOCKED` means a required safe dependency such as test PostgreSQL,
isolated Redis or Chromium is unavailable. A smaller substitute must not be
reported as a complete-gate pass.

GitHub Actions owns runner images, service containers, dependency bootstrap,
parallelism, caches, artifacts, permissions, environment authorization and
commit statuses. The security workflow invokes stable gate IDs and retains
parallel jobs. Its aggregate `platform-security-build=success` status for the
exact committed SHA remains the release authority; a local pass is neither
necessary nor sufficient for deployment.

## Production and performance boundaries

Deployment smoke runs only after immutable deployment activation and checks
that the exact SHA started and critical interfaces are alive. Live public and
destructive user QA are separate operator contours with their own identities,
confirmation and cleanup rules.

The canonical load-profile registry is `platform/performance/`.
Profiles record fixture shape, logical actions, HTTP attempts, concurrency,
spread/ramp, retry semantics, expected statuses, correctness, latency budgets,
resource evidence and cleanup. The external HTTP generator runs on the
GitHub-hosted runner. The production host performs only bounded fixture,
observer and exact-cleanup work; it never generates the measured client load.

Every retained result identifies its source SHA, profile ID/version/digest,
runner, fixture shape, offered logical actions, HTTP attempts and acceptance
outcome. A performance result is incomplete unless correctness and exact
cleanup pass. Canceled runs use the matching abort/cleanup workflow keyed by
the exact run ID.

## Contract self-test

The `docs` gate checks document shape, repository-local links and project skill
frontmatter/interface metadata. `verification-contract` checks registry/CI membership, workflow gate names,
direct command duplication, exclusion bypasses, backend discovery, hermetic
suite registration, production reachability from `ci`, documentation gate
IDs, load-profile schema/deduplication, workflow-owned load budgets and the
external-generator topology. Keep it deterministic and small enough to run
on every CI change.
