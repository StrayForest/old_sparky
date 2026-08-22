# Platform test-suite governance

- Status: Active procedure
- Owner: Platform maintainers
- Last reviewed: 2026-08-22

This document is the executable owner contract for the platform test suite. A
test must belong to exactly one group and each group must have one documented
runner. The groups describe behavior and environment, not coverage targets.
The machine-readable group manifest is `platform/tests/test-suite-manifest.json`.

## Test groups

| Group | Owner | Runner | Environment |
| --- | --- | --- | --- |
| `backend-unit` | API/domain owners | `tools/platform_run_tests.sh` with focused `unittest` selectors | test PostgreSQL/Redis |
| `backend-integration` | API/domain owners | `tools/platform_run_tests.sh discover -s tests` | test PostgreSQL/Redis |
| `migration` | persistence owner | `tools/platform_migration_scenario.py` | disposable PostgreSQL, first at `20260821_0039` |
| `web-hermetic` | web owner | `npm run test:hermetic` | mocked API and standalone Next.js |
| `server-smoke` | release owner | `platform_deploy_smoke.py` and release preflight | deployed server |
| `live-public` | production operator | `platform_live_browser_qa.sh public` through `platform-live-launch.yml` | canonical production origin, dedicated QA UID |
| `live-user-destructive` | production operator | approved `platform_live_user_qa.sh` workflow only | production, marked fixture data and mandatory cleanup |

The ordinary CI workflow runs all deterministic backend, migration, web
hermetic, typecheck, lint and build checks. Live-user and destructive browser
journeys remain explicit release/operator gates and are never silently hidden
by a grep exclusion.

## Local commands

From `platform/`, with `PLATFORM_ENVIRONMENT=test` and
`platformdb_test` configured:

```bash
tools/platform_run_quiet.sh "platform tests" -- \
  tools/platform_run_tests.sh discover -s tests
tools/platform_run_quiet.sh "migration scenario" -- \
  .venv_platform/bin/python tools/platform_migration_scenario.py
tools/platform_run_quiet.sh "web hermetic" -- \
  tools/platform_web_npm.sh --prefix apps/platform_web run test:hermetic
tools/platform_run_quiet.sh "web typecheck" -- \
  tools/platform_web_npm.sh --prefix apps/platform_web run typecheck
tools/platform_run_quiet.sh "web lint" -- \
  tools/platform_web_npm.sh --prefix apps/platform_web run lint
tools/platform_run_quiet.sh "web build" -- \
  tools/platform_web_npm.sh --prefix apps/platform_web run build
```

The migration scenario is destructive to its disposable database. It must not
be pointed at `platformdb` or a production connection. The scenario creates a
legacy `private` tournament and an intentionally duplicated active workflow
row, confirms the first upgrade fails without applying the revision, repairs
the duplicate, retries the upgrade, and verifies normalized visibility and the
final constraints.

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
