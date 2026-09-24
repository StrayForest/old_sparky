# Platform deployment runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-09-24

Use this document for the normal immutable release path. CSP mode changes and production browser/live-user evidence are intentionally isolated in [`csp-live-qa-runbook.md`](csp-live-qa-runbook.md); do not load that document for routine releases.

## Preconditions

1. Work from a clean, reviewed commit; release metadata records the exact
   GitHub target SHA.
2. Push the reviewed commit to `dev` and wait for the GitHub Actions
   `Platform security and build` gate. A successful full-route push run for the
   current `dev` HEAD is the normal production release signal and is consumed
   by the automatic deployment workflow; a docs-only or out-of-scope run is a
   successful non-deployable no-op. Do not substitute a manually run local test.
3. Confirm migration expand/rollback compatibility.
4. Confirm services are healthy, disk has at least 5 GiB available and is at
   most 85% conservative use, and `current`/`previous` releases are protected.
5. Create a fresh restore-verified backup.

The active GitHub `Protect dev` ruleset does not require a pull-request
approval for merge. It still protects the branch against deletion and
force-push, and the exact-SHA security/build status plus automatic production
deployment chain remain mandatory release gates.

## Normal production deploy through GitHub Actions

Normal production deployment is automatic after the reviewed commit is pushed
to `dev`. The chain is:

1. `Platform security and build` runs for the push and publishes the
   `platform-security-build` commit status plus an exact classifier artifact
   containing its schema/version, target SHA, expected gates and digest.
2. `Platform production auto-deploy` receives the completed workflow event only
   for a push to `dev`.
3. The auto-deploy gate downloads the classifier artifact from that exact
   security run, validates its schema, digest, target SHA and non-fallback
   deployable `full` route, then re-reads the current `dev` HEAD and refuses a
   stale successful CI result. The source run and both status snapshots are
   checked by the shared dependency-free
   [`platform_workflow_provenance.py`](../tools/platform_workflow_provenance.py)
   validator, including the exact repository/workflow/run attempt, SHA, event,
   branch, conclusion, trusted actor, description and attempt URL. The gate
   reads GitHub's paginated [list commit statuses endpoint](https://docs.github.com/en/rest/commits/statuses#list-commit-statuses-for-a-reference)
   (`/commits/{sha}/statuses`), retaining each raw row and its full `creator`
   object. It then requires `platform-security-build=success` and skips a SHA that already
   reports `platform-production-deploy=success` only when the matching
   successful deploy attempt has its exact bot-authored marker.
4. When those checks pass, the auto-deploy workflow dispatches
   `Platform production deploy` with `mode=deploy` on `dev`.
5. A secret-free prerequisite independently downloads and validates the exact
   classifier artifact before the expensive candidate build is allowed to run.
   The production environment then repeats that exact-SHA validation immediately
   before its first production write, followed by the security/build check and
   immutable artifact consumption. The classifier artifact is treated as
   bounded mode-0600 JSON data, never executed as Python; malformed, oversized,
   stale or non-deployable data aborts closed without printing the payload.
   Both checks execute one canonical parser from an immutable trusted `dev`
   checkout, never candidate source. Immediately before its first
   production-host write, the workflow re-reads the authoritative `dev` branch
   head. If `dev` moved from `TARGET_SHA` (the A→B race), the workflow aborts
   closed; only then does it transfer and install the artifact and run
   production smoke.
6. Before the expensive release build, `build-host-tools` creates and attests
   the deterministic release-independent host-tools handoff. The
   environment-approved `host-capability-preflight` verifies that exact
   artifact and the already provisioned
   `/opt/oldsparky/platform/shared/host-tools/<TARGET_SHA>` generation. It
   requires the configured SSH identity to be root and checks the generation's
   owner, mode, link count, type, capabilities and every digest with fixed
   absolute tools. This is a read-only gate: it never SCPs or executes the
   bundle and fails before release build, attestation, pending status or
   production artifact transfer when the generation is absent or mismatched.
   The one-time out-of-band provisioning and rollback procedure is the owner of
   [`production-host-tools-provisioning.md`](adr/production-host-tools-provisioning.md).

The security workflow also has a separate `workflow_run` status finalizer. It
uses only `statuses: write`, no checkout or secrets, and always overwrites the
`platform-security-build` context for the completed run's exact `head_sha` and
`/attempts/<run_attempt>` URL. Only a `success` conclusion publishes the fixed
description `Platform security and build passed`; every other conclusion is a
failure with the fixed description `Platform security or build failed`.
Because the write is idempotent and does not inspect older statuses, repeated
or superseded attempts cannot preserve a stale result. GitHub may suppress the
`workflow_run` event during an outage or force-cancel; the bounded operator
recovery is to wait five minutes, then inspect the exact run and commit status.
If the status is still pending, an authorized repository maintainer may post a
failure status for that exact SHA with `gh api
repos/StrayForest/old_sparky/statuses/<sha> -f state=failure -f
context=platform-security-build`; never post success manually.

The dispatch `mode`, runtime profile, release slug, target SHA and artifact
directory are checked by the bounded ASCII input guard before production host
access or secret-file setup. They cross SSH only as a mode-600 JSON handoff to
the fixed remote dispatcher; the host revalidates them before invoking the
deployment supervisor. Deploy handoffs also require canonical positive decimal
`classifier_run_id`/`classifier_run_attempt` values and the exact
`web_compression=enabled|disabled` choice. Load-cleanup evidence is projected into its closed
public schema before the private export inventory is removed, and an unknown,
symlinked, special or leftover export entry fails the workflow.

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

The production Alembic wrapper keeps the exact `upgrade head` allowlist and,
after the release transaction has quiesced writers, runs the catalog recovery
helper. The helper acts only when `alembic_version` is exactly `20260901_0050`
and a table matching the historical 0051 schema is present. It validates the
table and constraints, idempotently backfills the projection, and repairs only
invalid/unfinished concurrent indexes before stamping 0051. A valid index with
the wrong definition or table, or any incompatible table/constraint, fails
closed. Revision `20260913_0053` provides the same validation/repair as a
forward migration for databases that already recorded 0051/0052; no downgrade
or automatic migration reversal is performed.

### Manual workflow fallback

`Platform production deploy` keeps `workflow_dispatch` as an operator fallback,
not as the normal release path. Use manual dispatch only when an operator has an
explicit reason to repeat preflight/deploy for the current reviewed `dev` HEAD
or when diagnosing the automatic contour. The same exact-SHA
`platform-security-build=success` and deployable classifier artifact gates still
apply to `mode=deploy`; provide the originating security `run_id` and
`run_attempt`. A missing, malformed, fallback or non-deployable manifest blocks
deployment. `mode=preflight` remains available without that release artifact
guard and performs no install.

Both `mode=deploy` and the read-only `mode=preflight` require the immutable
host-tools capability gate. Manual dispatch cannot provision or repair that
generation: operators must use the approved out-of-band host-image/console
procedure, retain the previous valid generation, and repeat the reviewed
`dev` operation only after the exact post-copy inventory passes. There is no
workflow self-installer or legacy `current/tools` fallback for the production
dispatcher.

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
  -f runtime_profile=ready-vote-static-8 \
  -f classifier_run_id=<SECURITY_RUN_ID> \
  -f classifier_run_attempt=<SECURITY_RUN_ATTEMPT>
```

Use the `run_id` and `run_attempt` from the exact completed successful
`Platform security and build` push run for the same `dev` SHA. The deploy
workflow queries that run's workflow identity, event, branch, SHA, completion
and conclusion, then requires the `platform-security-build` status to point to
the same run URL; a missing, stale or mixed-attempt pair is rejected.

The recovery workflow does not change the active release, database, Redis,
Nginx configuration or application data. If the restart fails, its journal
output is the diagnostic handoff; do not weaken the preflight gate.

## Release state, activation and recovery

The workflow's guarded wrapper acquires the release-independent lock at
`/run/lock/oldsparky-platform-release.lock` before preflight and holds it
through staging, migration, activation, smoke and abort/recovery. This stable
lock is created/validated before a first bootstrap creates
`APP_DIR/releases/shared`; installer, deploy, rollback, runtime restore and
recovery use this same identity (with an inherited FD when nested). Before the
first stop or stage side effect it atomically writes
`shared/.release-operation.json` in `phase=quiesce-pending` with the original
API/worker/web/timer state, pointer identities and candidate path. After
staging, that same receipt is promoted to the operation schema before
migration. Never print service environments or secrets.

The production deploy workflow acquires the release lock before the retained
load lock (`/run/lock/oldsparky-retained-load-matrix.lock`) and keeps both
through candidate activation, runtime-profile changes, service
restart/readiness, final smoke and the commit boundary. Retained-load and
cleanup take only the load lock; storage maintenance acquires release then
load; rollback, release recovery and service recovery take only the release
lock. This is the only lock ordering and has no reverse edge. Release recovery
passes its inherited release-lock file descriptor through
rollback and runtime restoration, so Nginx and readiness are not changed after
the lock is released.

The wrapper leaves a durable transaction until migration, restart/readiness,
Nginx apply and both smoke paths pass. It prepares service-owned runtime paths
before restart and refreshes scoped env files. A rollback or recovery runtime
restore installs unit files with `PLATFORM_ENABLE_SYSTEMD_UNITS=0`; restoring
unit files never implicitly enables or starts a service or timer. The normal
activation path owns the reviewed health, Cloudflare and maintenance timer
enablement, and installs the off-site-backup unit/timer without silently
enabling off-site backup before its manual restore-drill gate.

If candidate activation fails, the workflow records read-only filesystem,
inode, mount and API sandbox facts, plus a sanitized systemd snapshot and the
last three minutes of API, worker and web journals before retaining the receipt
for the documented recovery decision.

Use `release-state-machine.md` for phase-specific recovery. An ERR/TERM/INT
after the snapshot may restore the old runtime and only services that were
active before quiesce, but it retains the migration receipt and never performs
an Alembic downgrade. Abort restores units and Nginx without an unconditional
restart, then checks only services that were active before quiesce. Intentionally
inactive services and timers remain stopped. A pointer, identity, restart or
readiness mismatch retains the receipt for another guarded attempt.

If an operator explicitly chooses code/runtime rollback after reviewing
database compatibility, use the guarded abort command. It restores the
recorded pointers and venv and never downgrades Alembic:

```bash
tools/platform_release_deploy.sh \
  --abort-retained \
  --confirm-migration-not-reversed \
  --app-dir /opt/oldsparky/platform
```

The command restores and verifies only services recorded active before
quiesce; intentionally inactive units and timers remain stopped. A restart,
readiness, pointer or identity failure retains the receipt for another guarded
attempt.

Rollback has a separate root-owned
`shared/.release-systemd-state.json` receipt. Before switching pointers it
records the exact active (`active|inactive`) and enablement
(`enabled|disabled|static`) state of every unit owned by the platform unit
installer. The receipt is validated against the original release identities
and is retained on an installer, restart, smoke or interruption failure.
Recovery restores only that closed owned set, first without `--now` enablement
and then to the recorded active state; an unsupported or malformed state fails
closed. `--no-restart` installs the files with activation disabled and verifies
the recorded active state without starting units. The receipt is removed only
after the rollback transaction has completed successfully.

The production abort workflow applies the same receipt authority: it accepts
only a validated v2 transaction (or the exact pre-quiesce receipt), selects a
release whose identity contract includes the v2 recovery implementation, and
checks every API/worker/web unit and the Cloudflare timer against the durable
pre-quiesce snapshot. It does not require intentionally inactive units to be
active, and it fails closed while retaining the receipt if recovery, pointer,
identity, or readiness evidence is incomplete.

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
