# Platform deployment runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-09-01

Use this document for the normal immutable release path. CSP mode changes and production browser/live-user evidence are intentionally isolated in [`csp-live-qa-runbook.md`](csp-live-qa-runbook.md); do not load that document for routine releases.

## Preconditions

1. Work from a clean, reviewed commit; release metadata records the exact
   GitHub target SHA.
2. Push the reviewed commit to `dev` and wait for the GitHub Actions
   `Platform security and build` gate. A successful push run for the current
   `dev` HEAD is the normal production approval signal and is consumed by the
   automatic deployment workflow; do not substitute a manually run local test.
3. Confirm migration expand/rollback compatibility.
4. Confirm services are healthy, disk has at least 5 GiB free and is below 85%, and `current`/`previous` releases are protected.
5. Create a fresh restore-verified backup.

## Normal production deploy through GitHub Actions

Normal production deployment is automatic after the reviewed commit is pushed
to `dev`. The chain is:

1. `Platform security and build` runs for the push and publishes the
   `platform-security-build` commit status for the exact SHA.
2. `Platform production auto-deploy` receives the completed workflow event only
   for a push to `dev`.
3. The auto-deploy gate re-reads the current `dev` HEAD and refuses a stale
   successful CI result. It also requires `platform-security-build=success` and
   skips a SHA that already reports `platform-production-deploy=success`.
4. When those checks pass, the auto-deploy workflow dispatches
   `Platform production deploy` with `mode=deploy` on `dev`.
5. The production workflow repeats the exact-SHA security/build check before
   packaging, SSH or any production release side effect, then builds, attests,
   transfers and installs the immutable artifact and runs production smoke.

Do not manually dispatch the deploy workflow for a normal `dev` push. Observe
the automatic chain and wait for the exact target SHA to finish:

```bash
gh run list \
  --repo StrayForest/old_sparky \
  --workflow platform-security.yml \
  --branch dev \
  --limit 5

gh run list \
  --repo StrayForest/old_sparky \
  --workflow platform-production-autodeploy.yml \
  --branch dev \
  --limit 5

gh run list \
  --repo StrayForest/old_sparky \
  --workflow platform-production-deploy.yml \
  --branch dev \
  --limit 5

gh run watch <run-id> --repo StrayForest/old_sparky --exit-status
```

The deploy workflow checks out the exact GitHub commit, builds the immutable
release and wheelhouse in CI, publishes and attests the artifact, verifies its
digest and source commit, then transfers that exact artifact to production.
The VPS performs no source checkout or dependency/build resolution; it only
revalidates the artifact and invokes the guarded release state machine. Record
the Actions run URL/ID, target SHA, release slug and final smoke result in the
handoff.

### Manual workflow fallback

`Platform production deploy` keeps `workflow_dispatch` as an operator fallback,
not as the normal release path. Use manual dispatch only when an operator has an
explicit reason to repeat preflight/deploy for the current reviewed `dev` HEAD
or when diagnosing the automatic contour. The same exact-SHA
`platform-security-build=success` gate still applies to `mode=deploy`.

For a read-only production gate without an install, an operator may dispatch
`mode=preflight` explicitly. A manual fallback must never be used to bypass a
pending, failed, missing or stale security/build result.

Do not run `platform_build_release.sh` or `platform_release_deploy.sh` directly
for a normal release. Those commands are implementation details of the
workflow; direct server execution is limited to an explicitly authorized
recovery or rollback.

### Service preflight recovery

If the deploy preflight reports that `deadlock-web` is not active, do not
disable the service check or invoke the release installer directly. Use the
operator-only service recovery workflow, which takes the release lock, refuses
to run during a retained release transaction, restarts only `deadlock-web`,
and verifies the existing active release on port 3000:

```bash
gh workflow run platform-production-service-recovery.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f confirmation=RECOVER-DEADLOCK-WEB
```

After the recovery workflow passes, repeat the production deploy for the same
reviewed `dev` HEAD using the explicit operator fallback, with the reason that
the automatic deploy was stopped by the service preflight:

```bash
gh workflow run platform-production-deploy.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f mode=deploy \
  -f runtime_profile=ready-vote-static-8
```

The recovery workflow does not change the active release, database, Redis,
Nginx configuration or application data. If the restart fails, its journal
output is the diagnostic handoff; do not weaken the preflight gate.

## Release state, activation and recovery

The workflow's guarded wrapper leaves a durable transaction until migration, restart/readiness,
Nginx apply and both smoke paths pass. It prepares service-owned runtime paths
before restart, refreshes scoped env files, enables the reviewed health,
Cloudflare and maintenance timers, and installs the off-site-backup unit/timer
without silently enabling off-site backup before its manual restore-drill gate.
Never print service environments or secrets.

Use `release-state-machine.md` for phase-specific recovery. A retained state
after migration is an operator decision point, not an automatic rollback.

If an operator explicitly chooses code/runtime rollback after reviewing
database compatibility, use the guarded abort command. It restores the
recorded pointers and venv and never downgrades Alembic:

```bash
tools/platform_release_deploy.sh \
  --abort-retained \
  --confirm-migration-not-reversed \
  --app-dir /opt/oldsparky/platform
```

## Smoke

```bash
cd /opt/oldsparky/platform/current

tools/platform_release_preflight.sh \
  --require-previous --require-verified-backup --require-edge-parity \
  --backup-max-age-hours 24

/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_deploy_smoke.py \
  --edge-origin https://127.0.0.1 \
  --edge-host old-sparky.com \
  --edge-insecure-loopback \
  --expected-csp-mode enforce

/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_deploy_smoke.py \
  --edge-origin https://old-sparky.com \
  --expected-csp-mode enforce
```

`--edge-insecure-loopback` is allowed only for loopback. Public smoke keeps normal certificate verification. The expected CSP mode must match the active release.

## Nginx-only changes

```bash
cd /opt/oldsparky/platform/current
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_install_nginx.py --json
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_install_nginx.py --apply --reload --json
```

Dry-run is the default. Apply only after policy validation and `nginx -t` succeed.

## Rollback

Use the shared recovery/rollback tooling and the exact previous release.
Rollback switches application release pointers; it does **not** automatically
downgrade Alembic. Do not bypass a missing or mismatched rollback receipt or
dependency-freeze proof. The rollback tool prepares a root-owned shared
recovery bundle and a compatibility handoff in the previous release before
switching `current`, so recovery remains available if the process dies after
the pointer switch.

If a rollback is interrupted, recover with the stable bundle (or the
`current/tools/platform_release_rollback.sh` shim, which delegates to it):

```bash
/opt/oldsparky/platform/shared/.release-recovery/platform_release_rollback.sh \
  --recover-pending \
  --app-dir /opt/oldsparky/platform
```

After rollback, repeat preflight plus origin/SNI and public smoke against the restored release.

A successful `Platform security and build` run for a push to the current
`dev` HEAD is expected to continue automatically into production deployment.
A stale successful run must be ignored, and a failed or missing gate must stop
the chain before production side effects.

## Special release contours

Open only when applicable:

- CSP candidate/enforcement changes, live browser QA, Turnstile/auth contour, AppArmor Chromium sandbox and CSP observation gates: [`csp-live-qa-runbook.md`](csp-live-qa-runbook.md).
- The live public browser workflow is owned by [`test-suite-governance.md`](test-suite-governance.md) and must invoke the dedicated server wrapper over SSH. Do not run production Playwright on the GitHub runner.
- Backup/restore: [`backup-restore-runbook.md`](backup-restore-runbook.md).
- Security policy and CSP ownership: [`security-runbook.md`](security-runbook.md).
- Incident response: [`incident-response.md`](incident-response.md).
