# Platform deployment runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-08-23

Use this document for the normal immutable release path. CSP mode changes and production browser/live-user evidence are intentionally isolated in [`csp-live-qa-runbook.md`](csp-live-qa-runbook.md); do not load that document for routine releases.

## Preconditions

1. Work from a clean, reviewed commit; release metadata records the exact
   GitHub target SHA.
2. Push the reviewed commit to `dev` and wait for the GitHub Actions security
   and build gate; do not substitute a manually run local test.
3. Confirm migration expand/rollback compatibility.
4. Confirm services are healthy, disk has at least 5 GiB free and is below 85%, and `current`/`previous` releases are protected.
5. Create a fresh restore-verified backup.

## Normal production deploy through GitHub Actions

Normal production deployment is initiated from GitHub Actions, never by an
agent directly invoking release scripts on the server. After the exact-SHA
security/build workflow is green, dispatch the reviewed `dev` branch and wait
for the run to finish:

```bash
gh workflow run platform-production-deploy.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  --field mode=deploy

gh run list \
  --repo StrayForest/old_sparky \
  --workflow platform-production-deploy.yml \
  --branch dev \
  --limit 5

gh run watch <run-id> --repo StrayForest/old_sparky --exit-status
```

For `mode=deploy`, the workflow fails closed until the exact target SHA has a
successful `platform-security-build` commit status. A pending, failed or
missing backend/security/build result cannot reach packaging, SSH or the
production release state machine.

Use `--field mode=preflight` when only the production gate is needed. The
deploy workflow checks out the exact GitHub commit, builds the immutable
release and wheelhouse in CI, publishes and attests the artifact, verifies its
digest and source commit, then transfers that exact artifact to production.
The VPS performs no source checkout or dependency/build resolution; it only
revalidates the artifact and invokes the guarded release state machine. Record
the Actions run URL/ID, target SHA, release slug and final smoke result in the
handoff.

Do not run `platform_build_release.sh` or `platform_release_deploy.sh` directly
for a normal release. Those commands are implementation details of the
workflow; direct server execution is limited to an explicitly authorized
recovery or rollback.

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

The production workflow is manual-dispatch only while live edge proof remains
operator-owned. A successful CI build or branch push must not be treated as
production deployment approval.

## Special release contours

Open only when applicable:

- CSP candidate/enforcement changes, live browser QA, Turnstile/auth contour, AppArmor Chromium sandbox and CSP observation gates: [`csp-live-qa-runbook.md`](csp-live-qa-runbook.md).
- The live public browser workflow is owned by [`test-suite-governance.md`](test-suite-governance.md) and must invoke the dedicated server wrapper over SSH. Do not run production Playwright on the GitHub runner.
- Backup/restore: [`backup-restore-runbook.md`](backup-restore-runbook.md).
- Security policy and CSP ownership: [`security-runbook.md`](security-runbook.md).
- Incident response: [`incident-response.md`](incident-response.md).
