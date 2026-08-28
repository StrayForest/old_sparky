# Platform test-suite governance

- Status: Active procedure
- Owner: Platform maintainers
- Last reviewed: 2026-08-28

This document is the executable owner contract for the platform test suite. A
test must belong to exactly one group and each group must have one documented
runner. The groups describe behavior and environment, not coverage targets.
The machine-readable group manifest is `platform/tests/test-suite-manifest.json`.

## Test groups

| Group | Owner | Runner | Environment |
| --- | --- | --- | --- |
| `backend-unit` | API/domain owners | `platform-security.yml` / `backend` job (focused selectors when needed) | GitHub runner with test PostgreSQL/Redis |
| `backend-integration` | API/domain owners | `platform-security.yml` / `backend` job | GitHub runner with test PostgreSQL/Redis |
| `migration` | persistence owner | `platform-security.yml` / `Migration scenarios` job | GitHub runner with disposable PostgreSQL |
| `web-hermetic` | web owner | `platform-security.yml` / `Web hermetic` job | GitHub runner with mocked API and Chromium |
| `server-smoke` | release owner | `platform-production-deploy.yml` / `deploy` mode | production server over GitHub Actions SSH |
| `live-public` | production operator | `platform_live_browser_qa.sh public` through `platform-live-launch.yml` | canonical production origin, dedicated server QA UID |
| `live-user-destructive` | production operator | `platform-live-user-qa.yml` dispatches `platform_live_user_qa.sh` over SSH | production server; marked fixture data and mandatory cleanup |
| `retained-load-preprod` | performance operator | `platform-retained-load-matrix.yml` dispatches `platform_retained_load_matrix_qa.sh` over SSH | dedicated pre-production server; retained fixture data; explicit operator confirmation |
| `retained-load-production` | production/performance operator | `platform-production-retained-load-matrix.yml` and `platform-production-retained-load-cleanup.yml` over SSH | canonical live origin; retained fixture data; explicit production confirmation and exact cleanup |

The ordinary CI workflow runs all deterministic backend, migration, web
hermetic, documentation, typecheck, lint and build checks. Server-side smoke
and browser journeys are dispatched through GitHub Actions and execute on the
production server over the controlled SSH wrappers. Live-user and destructive
journeys remain explicit operator gates and are never silently hidden by a
grep exclusion. The retained load matrix is also an explicit operator gate,
but its target is a dedicated pre-production host and its API origin is
loopback on that host. It must not run on an ordinary GitHub-hosted runner:
the runner would measure its own CPU/network limits instead of the platform's
database, workers and host resources.

The retained matrix's 10,000 users are persisted scale-fixture accounts, not
10,000 simultaneous request workers. Its `matrix` profile exercises ordinary
tournament workflow writes and request-driven reads. Its `write-burst` profile
measures Ready Check vote POST contention after the server-known window. The
current product has no Ready Check or bracket SSE/polling profile; historical
transport results remain in the archived AS-19/AS-20 records only.

The production retained-load group is a deliberate exception to the normal
release gate: it is never scheduled, never part of ordinary CI, and never runs
on a GitHub-hosted runner. It uses the one production VPS through the canonical
`https://old-sparky.com` origin, so it measures the live edge, API, worker,
database and host resources. “There are no existing users” does not make this
safe to run casually: the test still consumes CPU/RAM/DB/Redis capacity,
publishes public tournaments, changes caches/metrics/logs and can affect site
availability while the matrix is active. Use an approved low-traffic window
and monitor the host during the run.

## GitHub execution

Run deterministic checks through the GitHub security workflow on the reviewed
`dev` ref:

```bash
gh workflow run platform-security.yml \
  --repo StrayForest/old_sparky \
  --ref dev
gh run watch <run-id> --repo StrayForest/old_sparky --exit-status
```

The migration scenario is destructive to its disposable database. It must not
be pointed at `platformdb` or a production connection. The scenario creates a
legacy `private` tournament and an intentionally duplicated active workflow
row, confirms the first upgrade fails without applying the revision, repairs
the duplicate, retries the upgrade through `20260824_0043`, and verifies
normalized visibility, participant-capacity slots and the final constraints.

Do not substitute a manually run local test for the GitHub workflow. Local
commands are implementation details for explicit CI-failure diagnosis only;
the GitHub jobs and their aggregate `platform-security-build` status are the
release authority.

### Retained pre-production load gate

The retained matrix is `workflow_dispatch` only. It creates approximately
10,000 retained synthetic users, 20 tournaments and 600 teams, so it has no
schedule and must not be added to ordinary CI or production deployment. The
workflow is an orchestrator: the actual API load and performance collection
run on a dedicated pre-production host through the fixed root supervisor
`/root/old_sparky/platform/tools/platform_retained_load_matrix_qa.sh`. The
supervisor verifies the reviewed commit, the active release provenance and a
non-production environment before it starts.

Configure the protected GitHub `preproduction` environment with these secrets:
`PREPROD_SSH_HOST`, `PREPROD_SSH_USER`, `PREPROD_SSH_KEY` and
`PREPROD_SSH_HOST_FINGERPRINT`. The SSH user may use passwordless sudo only for
the fixed supervisor. The host must have the reviewed `/root/old_sparky`
checkout, the matching `/opt/oldsparky/platform/current/RELEASE.json`, the
platform virtualenv and a loopback-accessible pre-production API.

Start it from the reviewed `dev` ref with an existing pre-production control
account:

```bash
gh workflow run platform-retained-load-matrix.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f confirmation=RUN-RETAINED-LOAD-MATRIX \
  -f control_email=<existing-preprod-account-email> \
  -f concurrency=80
gh run watch <run-id> --repo StrayForest/old_sparky --exit-status
```

The job summary shows pass/fail, completed users/tournaments, worst HTTP p95
and p99, bottleneck classes and the slowest client phases. The workflow
artifact contains only the compact `matrix-summary.json`, bounded execution
log and remote wrapper log. Detailed per-tournament reports stay on the
pre-production host under
`/opt/oldsparky/platform/shared/preprod-retained-matrix/gha-<run-id>/` and are
not copied to GitHub Actions, because they can contain invite codes and
operational identifiers. After manual inspection, use the marked
pre-production cleanup procedure; do not delete the retained data
automatically from the workflow.

### Retained production load and exact cleanup

The live matrix is a manual operator gate. It creates approximately 10,000
synthetic `@example.com` users, 20 retained tournaments and 600 teams through
the real API workflow: profile and captain-profile saves, changed saves,
public registration, invite-code claim plus registration, ready-check,
captain round, assignment and bracket creation. The existing control account
is only joined to selected tournaments; its profile and credentials are not
changed. The workflow summary prints direct browser links for its registered,
ready-check and assigned-team cases.

Run it only from `dev` after the reviewed commit has been deployed:

```bash
gh workflow run platform-production-retained-load-matrix.yml \
  --repo StrayForest/old_sparky --ref dev \
  -f confirmation=RUN-PRODUCTION-RETAINED-LOAD-MATRIX \
  -f control_email=aleksei.lisitsin1@gmail.com -f concurrency=80
gh run watch <load-run-id> --repo StrayForest/old_sparky --exit-status
```

The workflow supports `profile=matrix` and `profile=write-burst`. The matrix
profile is the ordinary retained data-volume/workflow run. The write-burst
profile uses the server-known Ready Check window and measures vote writes; it
does not create persistent client connections or polling traffic. The former
browser-polling, `sse` and `combined` profiles are retired and are not current
release gates.

The historical Ready Check SSE staircase and compatibility-bracket transport
measurements remain linked only as audit evidence. They must not be presented
as current product behavior or a Ready Check capacity target.

The detailed reports remain on the VPS under
`/opt/oldsparky/platform/shared/production-retained-matrix/gha-<load-run-id>/`
until cleanup. The Actions artifact contains only the compact summary and
bounded logs; it does not expose invite codes.

After opening the summary links and manually checking the site as the control
account, run the separate exact cleanup workflow with that load workflow's ID:

```bash
gh workflow run platform-production-retained-load-cleanup.yml \
  --repo StrayForest/old_sparky --ref dev \
  -f confirmation=DELETE-PRODUCTION-RETAINED-LOAD \
  -f load_run_id=<load-run-id> \
  -f control_email=aleksei.lisitsin1@gmail.com
gh run watch <cleanup-run-id> --repo StrayForest/old_sparky --exit-status
```

If a retained production load is canceled while its SSH step is active, run
`platform-production-retained-load-abort.yml` first with the exact canceled
load run ID. The load supervisor has a 180-minute remote ceiling and the abort
workflow terminates only that run's process tree, then verifies the shared
lock is available. Cleanup can recover a missing browser report from the
durable `PreprodTestRun.report`; it remains fail-closed and deletes nothing if
the recovered identity is incomplete or overlaps unrelated data.

Cleanup deletes only the selected run's marked synthetic users and their
tournaments; tournament participants, invites, ready-check/captain/assignment
rows, brackets, sessions, audit rows and eligible media metadata are removed
through the exact graph and database cascades. It preserves the control
account and all unrelated production data. If any identity, marker, report
path, ownership or cross-run check fails, cleanup stops before deletion.
The database keeps only the exact `PreprodTestRun` row marked `cleaned` as an
operator trace; it contains no live account credentials or active fixture
rows.

## Production browser gate

The GitHub workflow does not install Playwright or run production browsers on a
runner. It connects to the production host with the deployment SSH identity
and invokes the fixed root supervisor from `/root/old_sparky`:

```bash
PLATFORM_APP_DIR=/opt/oldsparky/platform \
PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json \
PLAYWRIGHT_LIVE_BASE_URL=https://old-sparky.com \
platform/tools/platform_live_browser_qa.sh public
```

The supervisor owns the machine lock, runtime cache, Chromium sandbox and
dedicated `oldsparky-liveqa` UID. The workflow captures its bounded output and
requires the `LIVE_BROWSER_QA_SUCCESS` marker before publishing the report.
Direct root Playwright, `--no-sandbox`, arbitrary production URLs and runner-
side production browser execution are invalid.

If the production host has not yet been provisioned, the first workflow run
may explicitly create the root-only CSP QA bundle with a fresh marker. This
mode refuses to replace an existing bundle and does not print generated
credentials:

```bash
gh workflow run platform-live-launch.yml \
  -f base_url=https://old-sparky.com \
  -f provision=true \
  -f marker=liveqa-csp-candidate-<unique>
```

After that one-time provisioning, run the public gate without provisioning:

```bash
gh workflow run platform-live-launch.yml \
  -f base_url=https://old-sparky.com \
  -f provision=false
```

## Adding or moving a test

1. State the production behavior and failure path the test protects.
2. Assign one group in the test manifest/CI command; do not add a grep
   exclusion to hide a failing deterministic test.
3. For workflow, permission, migration or concurrency behavior, assert the
   persisted final state and the negative path, not only the HTTP response.
4. Update this document only when the runner contract changes. Record detailed
   implementation evidence in an archive document after release.
