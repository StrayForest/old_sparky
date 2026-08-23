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
| `backend-unit` | API/domain owners | `platform-security.yml` / `backend` job (focused selectors when needed) | GitHub runner with test PostgreSQL/Redis |
| `backend-integration` | API/domain owners | `platform-security.yml` / `backend` job | GitHub runner with test PostgreSQL/Redis |
| `migration` | persistence owner | `platform-security.yml` / `Migration scenarios` job | GitHub runner with disposable PostgreSQL |
| `web-hermetic` | web owner | `platform-security.yml` / `Web hermetic` job | GitHub runner with mocked API and Chromium |
| `server-smoke` | release owner | `platform-production-deploy.yml` / `deploy` mode | production server over GitHub Actions SSH |
| `live-public` | production operator | `platform_live_browser_qa.sh public` through `platform-live-launch.yml` | canonical production origin, dedicated server QA UID |
| `live-user-destructive` | production operator | `platform-live-user-qa.yml` dispatches `platform_live_user_qa.sh` over SSH | production server; marked fixture data and mandatory cleanup |

The ordinary CI workflow runs all deterministic backend, migration, web
hermetic, documentation, typecheck, lint and build checks. Server-side smoke
and browser journeys are dispatched through GitHub Actions and execute on the
production server over the controlled SSH wrappers. Live-user and destructive
journeys remain explicit operator gates and are never silently hidden by a
grep exclusion.

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
the duplicate, retries the upgrade, and verifies normalized visibility and the
final constraints.

Do not substitute a manually run local test for the GitHub workflow. Local
commands are implementation details for explicit CI-failure diagnosis only;
the GitHub jobs and their aggregate `platform-security-build` status are the
release authority.

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
