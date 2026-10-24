# Platform deployment runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-10-24

Use this document for the normal immutable release path. CSP mode changes and production browser/live-user evidence are intentionally isolated in [`csp-live-qa-runbook.md`](csp-live-qa-runbook.md); do not load that document for routine releases.

## Preconditions

1. Start from a clean, reviewed commit; release metadata records its exact GitHub target SHA.
2. Push it to `dev`; record the exact Security run ID/attempt/SHA and verify its classifier is full, deployable and
   non-fallback for this normal app release. Global-guidance routes do not qualify; documented recovery, bootstrap,
   candidate and docs routes use their own procedures.
3. While Security is active and the shared slot unowned, the operator dispatches one `create` backup with that exact
   ID/attempt/SHA. Require `security_started <= backup.created_at <= backup.run_started_at <= backup_job.started_at < security_last_job_completed`.
   Confirm the backup workflow and production job actually start before that bound; a queued run that starts later
   cannot qualify. Do not wait for CI to finish or substitute an ID, attempt, SHA or timestamp; see the [backup procedure](backup-restore-runbook.md#local-verified-backup).
4. Wait for exact Security success and the restore-verified receipt. Auto-deploy starts after Security completes; it never dispatches the backup, only polls it and dispatches the child after both proofs pass.
5. Confirm migration compatibility, healthy services and at least `max(5 GiB, 10% of total)` free / at most 90% used; protect `current`/`previous`. Never manually deploy. Other routes use their own procedures; local tests do not replace release gates.

If missed, first confirm that the original SHA is still current `dev`. Then rerun all jobs of that push's Security run (not failed jobs only), record its new attempt, and reserve a backup with the same run ID, new attempt and SHA while it runs. If `dev` moved, stop and replan from current `dev` and its own Security run. This creates a new proof; it does not backfill the completed attempt.

GitHub's active `Protect dev` ruleset requires no PR approval, but prevents branch deletion/force-push; exact-SHA
security/build and the automatic production chain remain mandatory release gates.

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
   route, then consumes its `deployable` bit: a deployable `full` route
   proceeds while a valid full CI-only route completes as a no-op. Recovery-bootstrap-only or
   strict docs-only routes stay non-deployable; the automatic chain may send either to
   `baseline-reconcile` to authenticate the active baseline and full baseline-to-target range.
   Reconcile is a verified no-op for an allowed cumulative no-op; app releases require a
   deployable full cumulative route with every canonical gate, no fallback, and exact-target
   runtime proof. Every deployable full push to `dev` runs both exact-target runtime gates,
   so a later test-only push cannot suppress proof required by runtime changes earlier in
   the undelivered range. A storage no-op is allowed only for an
   authenticated recovery input and a range wholly in the closed recovery,
   storage and docs sets; it changes no classifier field and authorizes no app
   work. Other ranges need normal release checks. Changes to `platform_production_deploy_supervisor.sh` require a deployable full route and exact-target release, not the recovery-bootstrap no-op. The gate re-reads current `dev` HEAD and refuses a stale successful CI result.
   The source run and both status snapshots are checked by the shared dependency-free [`platform_workflow_provenance.py`](../tools/platform_workflow_provenance.py)
   validator, including the exact repository/workflow/run attempt, SHA, event,
   branch, conclusion, trusted actor, description and attempt URL. The gate
   reads GitHub's paginated [list commit statuses endpoint](https://docs.github.com/en/rest/commits/statuses#list-commit-statuses-for-a-reference)
   (`/commits/{sha}/statuses`), retaining each raw row and its full `creator`
   object. It then requires `platform-security-build=success` and skips a SHA that already
   reports `platform-production-deploy=success` only when the matching
   successful deploy attempt has its exact bot-authored marker.
   Before child dispatch, the guard revalidates the exact Security run/attempt/source and requires complete, terminal, source-bound pages from that attempt's [workflow-jobs endpoint](https://docs.github.com/en/rest/actions/workflow-jobs), with valid start/completion times. It derives the conservative `last_job_completed_at` bound as the maximum job `completed_at`.
   The reserved backup workflow and production job must start strictly before that bound; `updated_at` is only a consistency upper bound, and event `workflow_run.completed_at` is never a fallback. Missing, malformed or incomplete jobs abort. This bound may reject starts after the final job but before overall workflow completion; it is not the exact workflow-finish time.
4. The automatic chain dispatches `mode=deploy` for deployable full routes and
   `mode=baseline-reconcile` for validated recovery-bootstrap-only or docs-only
   routes; never dispatch either mode manually. The production workflow
   authenticates source/caller, validates the cumulative range and active
   baseline's latest bot-authored deploy marker, attempt URL and successful exact
   `Deploy production` job. Legacy slugs require root validation, GitHub attempt 1
   and passing exact checks; unknown formats, reruns and ambiguity fail closed.
   An allowed docs-only no-op publishes an authenticated source receipt binding
   workflow SHA to active app baseline without deployment. Wait for it before
   live QA, which targets the unchanged app. Recovery-bootstrap build/publisher
   separately emits its own authenticated skip receipt, not the QA receipt.
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
   head. If `dev` moved from `TARGET_SHA`, the workflow aborts
   closed; only then does it transfer and install the artifact and run
   production smoke.
6. Before the expensive release build, `build-host-tools` resolves the
   repository-owned `platform/contracts/host_tools_pin.json` from the exact
   target source, validates its repository/commit ancestry and closure
   baseline, then checks out and runs the pinned `HOST_TOOLS_SHA` helper to
   create and attest the deterministic release-independent host-tools handoff. Auto-deploy keeps full history in its exact-source checkout for ancestry validation, disables persisted checkout credentials, and reports missing pinned ancestors as a shallow-history failure without weakening validation.
   The environment-approved `host-capability-preflight` verifies that exact
   artifact and the already provisioned
   `/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>` generation. It
   requires the configured SSH identity to be root and checks the generation's
   owner, mode, link count, type, capabilities and every digest with fixed
   absolute tools. The 15-member, four-component candidate adds
   `cpu_diagnostic_plan_control=1`; its contract is in the [host-tools ADR](adr/production-host-tools-provisioning.md).
   The closure remains inactive until pinning and deployment; dispatchers use
   `/usr/bin/python3.12 -I -B`.
   A missing or mismatched generation fails before app build/attestation,
   pending status or transfer.
   Host bundle/source bind to `HOST_TOOLS_SHA`; app artifact, provenance,
   migration and receipt bind to `TARGET_SHA`. API artifact ID/name/run/source/digest and downloaded size
   are cross-checked by bounded canonical verification. Because a nested
   `workflow_run.run_attempt` may be absent, the secret-free builder resolves
   the exact attempt through GitHub's authoritative attempt endpoint.
   For installer/evidence/validator changes, follow the host-tools ADR's **C→P** sequence: backward-compatible validator C, pin-only P, exact-P CI and read-only preflight, signed bundle verification, out-of-band provisioning, installed self-test and successful exact-P preflight. If cumulative application changes cannot deploy with strict reuse, withhold P's backup so its automatic parent refuses before readiness/child dispatch; do not rerun that parent or use baseline reconciliation. A later app source enables fresh policy and follows normal Security, source-bound backup and automatic deployment. A pin or bundle is not installation proof. The ADR owns provisioning/rollback; shared-venv reuse and managed LiveQA runtime procedures are in the [CSP and live-QA runbook](csp-live-qa-runbook.md).

### Recovery-bootstrap baseline reconciliation

This automatic-only path authenticates exact source/caller/parent attempts, the pinned host baseline and its deployment proof, plus first-parent ancestry to `dev`.
Classification follows event ownership (the bundled live-QA installer stays full/runtime). Bootstrap-only is a verified no-op; deployable routes require canonical gates and exact-target runtime proof when sensitive.
The finalizer preserves push context. Cumulative baseline-to-target classification recognizes root `AGENTS.md` as a non-artifact companion only if removing it yields a canonical, deployable, runtime-sensitive `full` route.
The cumulative manifest still binds `AGENTS.md`; normal event classification, unknown paths and bootstrap-only no-op rules remain fail-closed.

Only marker-bound temporary ingress and lock files may precede comparison.
Under both locks, the immutable supervisor checks tuple match, no pending
transaction and unchanged `dev` before app artifact validation/extraction,
candidate execution or protected writes. Cleanup removes only bound ingress.
See the [baseline-reconcile ADR](adr/recovery-bootstrap-retained-abort.md#authenticated-baseline-reconciliation-for-bootstrap-only-source-ranges)
and [host-tools provisioning ADR](adr/production-host-tools-provisioning.md).

### Non-deployable pull-request host-tools candidate

The default-branch `Platform host-tools candidate` workflow is evidence-only.
It starts from a completed successful `Platform security and build`
`workflow_run` for one open, non-draft same-repository pull request targeting
`dev`; it is not a manual install or deployment entrypoint.  The workflow
checks out validators from an immutable trusted `dev` commit **T**, reads the
pull-request head **E** only as data, resolves the pinned host-tools
generation **C** from **E**, and requires the current base to be a strict
ancestor of **E** while `C ∈ reachable(E) \ reachable(base)`. This accepts a
merge-sync of current `dev` before a PR introduces **C**, but rejects a
generation already reachable from current `dev`; the exact pinned closure is
also required. Candidate source is never executed, imported or compiled.

Before attestation and again before upload it revalidates the exact security
run/attempt, PR number, head SHA/ref, base SHA/ref and canonical repository.
It attests only the deterministic inner bundle and read-backs both the
candidate and evidence artifact envelopes with bounded digest, size and
closed-member checks.  The evidence receipt is explicitly non-deployable;
production workflows do not consume either `platform-host-tools-candidate-*`
artifact prefix.  If this workflow fails, do not retry with a manual dispatch,
host access or production release; investigate the exact run and PR state.

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

The bounded ASCII input guard validates dispatch mode/profile, target SHA,
artifact directory, exact `gha-<run_id>-<run_attempt>-<first 12 lowercase
target-SHA characters>` slug, positive decimal classifier IDs/attempts and
`web_compression=enabled|disabled` before host access or secret setup. The
builder adds no timestamp; filename, archive metadata and supervisor retain the
slug. SSH carries it only in a mode-600 JSON handoff revalidated by the fixed
dispatcher. Cleanup evidence uses a closed public schema; unknown, symlinked,
special or leftover export entries fail the workflow.

On `ubuntu-latest`, the candidate builder uses setup-python 3.12 from a
validated private root-owned copy for its venv and canonical build, not runner
system Python.

Every production SSH boundary validates fresh scanner output and pre-existing
`known_hosts` files with the same parser. Fresh scans get two visible attempts;
each attempt is a fresh mode-600 file, bounded by an outer
four-second timeout and `ssh-keyscan -T 3`, and accepts exactly one nonblank,
noncomment record with the exact host token, `ssh-ed25519` type and pinned
SHA-256 fingerprint. OpenSSH 10.2 emits its version banner as a stdout comment (9.6 emitted it on stderr);
parsing ignores only blank/comment lines. Malformed, multiple or mismatched key records fail closed.
The 0.5-second backoff keeps the total acquisition budget at most 8.5 seconds. On failure,
diagnostics report only the scanner status, line/record counts and validation
booleans; they never print the host token or key material. Strict host-key
checking remains enabled and no TOFU or fallback path is permitted.

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

The deploy workflow checks out the exact commit, builds and attests the release
and wheelhouse, verifies digest and `TARGET_SHA`, then transfers it. Host control
comes only from reviewed `HOST_TOOLS_SHA`, never the secret-bearing app checkout. The VPS validates the
artifact and invokes the guarded state machine without source checkout or dependency resolution.
Record run URL/ID, target SHA, release slug, smoke result and host-tools SHA in the handoff and receipt.

Archives may carry `deploy/python-venv-policy.json` (`schema: 1`) with `transition: require_proven_reuse` or explicit `require_fresh_venv`.
`require_proven_reuse` keeps strict shared-venv proof, rejects dependency-skip requests and requires `max(5 GiB, ceil(10% of filesystem)) + 128 MiB` free; proof or space failure aborts before replacement or pointer changes. The source-bound normal release updates the reviewed systemd templates with the candidate; the exact-SHA Security gate and automatic deployment chain remain required. Do not pass one-off threshold overrides or install the unit outside that chain.
`require_fresh_venv` is explicit for a reviewed dependency upgrade only and never follows a failed reuse proof. It requires `RELEASE.json.python_venv_install_evidence` with `schema=1`, `builder_allocated_bytes` (1 byte–1 GiB), `wheelhouse_expanded_bytes` (1 byte–4 GiB), `host_overhead_bytes=134217728` and a 64-character lowercase `inputs_sha256`.
The input digest binds exact requirements, lock, freeze and wheelhouse manifest bytes; expanded wheelhouse size is the sum of ZIP member sizes. After extraction, free space must cover the base floor plus measured builder allocation, expanded wheels and the additional 128 MiB; `f_bavail` already includes the artifact, candidate and old venv.
Fresh install disables pip cache, puts `TMPDIR` inside the new venv, caps actual venv allocation at builder allocation plus 128 MiB and rechecks the base floor before transaction exchange. The old venv and rollback snapshot remain protected through activation.
Missing policy retains legacy behavior only for historical artifacts; neither explicit policy falls back after proof or capacity failure. Strict reuse follows a bounded, cycle-checked unchanged-release chain when no origin receipt exists, then writes an exclusive root-owned mode-`0600` receipt binding releases, shared-venv device/inode, freeze, wheelhouse and activation digests; later unchanged releases validate it without retaining the origin.

The production Alembic wrapper keeps the exact `upgrade head` allowlist. Its
candidate-bound read-only revision check runs under quiesced locks before
catalog recovery, per the [migration guard ADR](adr/candidate-forward-migration-guard.md).
The helper acts only when `alembic_version` is `20260901_0050` and the
historical 0051 table exists. It validates table/constraints, backfills the
projection idempotently and repairs only invalid concurrent indexes before
stamping 0051. Wrong definitions/tables or incompatible schema fail closed.
Revision `20260913_0053` applies the same repair forward for databases at
0051/0052; no downgrade or automatic reversal occurs.

The migration wrapper has a bounded 300-second outer operation deadline that
covers preflight, partial-0051 repair and the final `upgrade head` command. The
checked-in Alembic environment applies 30-second asyncpg connect and command
bounds plus PostgreSQL `statement_timeout` and `lock_timeout` to each
`current`, `heads` and `upgrade` command; recovery and preflight engines use
the same settings. Strict environment overrides may only remain within this
bounded contract; these are defense-in-depth within the same operation boundary. A timeout exits
nonzero (124 from `timeout(1)`) while retaining the durable
`migration-pending` receipt; operators must inspect the database and resume or
abort through the release state machine rather than retrying or downgrading.

The read-only health gate budgets four 10-second service probes, one 5-second
loopback readiness probe and one 10-second certificate probe (55 seconds),
with a 35-second margin in the systemd unit (`TimeoutStartSec=90s`). A timeout
is a failed health result, never a successful readiness signal.

### Manual workflow fallback

`Platform production deploy` keeps `workflow_dispatch` for diagnosis or a reviewed current `dev` HEAD; it is not the normal release path.
`mode=deploy` requires exact-SHA security success and a deployable classifier; provide its
source `run_id`/`run_attempt`. `mode=baseline-reconcile` is automatic-only;
`mode=preflight` performs no install.

`mode=recovery-deploy` is a separate operator exception for exact failed
attempt **R.A** proven by its bounded private plain-text job log to be the report-only invalid-marker case. Dispatch
against current reviewed **T** with exact failed and successful source-security
run/attempt pairs and `report_only_confirmation=RECOVERY_DEPLOY_REPORT_ONLY_INVALID_MARKER`.
All full security jobs, both real runtime jobs, the closed bootstrap classifier,
cumulative no-op check, **S11** baseline and lock-held tuple recheck are required.
It performs a fresh ordinary **T** build/install; only new **T** success is green and **R** remains
failed. Never rerun **R** or invoke an installer.
See the [recovery-bootstrap ADR](adr/recovery-bootstrap-retained-abort.md#operator-recovery-deployment-for-a-report-only-invalid-marker)
for the exact evidence and tuple contract.

Deploy/preflight require immutable host-tools and exact full CI on pin-bearing **P** descended from **C**.
Normally, read-only preflight at **P** builds and attests **C**. A natural bundle may also be
provisioned from the exact auto-parent during its active 1,800-second pre-child window, only for
current-`dev` **P** after the source-bound restore-verified backup, parent build/attestation/upload
and preceding host-contract checks succeed. No child, app write, pending state or competing operation may exist; provision before the deadline and bind the artifact to the exact parent run/attempt independently of the source Security attempt. The parent verifies **C** before child dispatch; see the [host-tools ADR](adr/production-host-tools-provisioning.md) for trust checks.
The separate child-failure path remains limited to capability failure solely because **C** was missing before build, pending state, transfer or protected writes. For that path only, after self-testing **C**, rerun all jobs on the exact auto-deploy run only while **P** remains `dev`, source proof and host tuple are unchanged, and no operation is pending. This creates a fresh child; verify its run IDs/attempts and success markers. Never manually deploy/reconcile or rerun a failed child.

After a verified pre-install failure, allow one all-jobs rerun only if the exact auto target remains
current `dev`, source CI succeeds, baseline is unchanged, no transaction is pending, locks are free,
and backup/edge/config/readiness/host checks pass. Require a fresh child; never rerun child/jobs or
manually dispatch. Stop if it fails again or the stage is unclear.
Do not run `platform_build_release.sh` or `platform_release_deploy.sh` directly for a normal release.
They are workflow implementation details; direct server execution is limited to explicitly authorized recovery or rollback.

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

The web mode above does not change the active release, database, Redis, Nginx configuration or application data. Failures emit only a closed `RECOVERY_FAILURE` stage and optional fixed class; raw remote stdout/stderr are deleted by the runner.
Use that bounded marker with the retained transaction receipt and systemd journal; do not retry or weaken preflight until the exact failure stage is verified.

For API/worker-only recovery on the current immutable release, follow the
[service recovery runbook](service-recovery-runbook.md).

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
migration. Never print service environments or secrets. With `--apply`, pass reviewed candidate slugs; under lock, set drift aborts before deletion. Current/previous and 14-day backup protections remain.

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
unit files never implicitly enables or starts a service or timer. On a clean
first install, the staged operation receipt first captures the exact inactive
and disabled state of the seven application/service timers. The initial
activation boundary then prepares unit files with enablement disabled, and only
after smoke restores that baseline, enables the intended units, verifies all
seven enabled/active states, reconciles the live-QA runtime, and reruns
readiness. Live-QA runtime reconciliation uses a separate 600-second maximum,
clamped against the exact candidate-supervisor deadline with a 600-second
post-reconcile reserve and timeout-reaping grace. The supervisor, remote
dispatcher and workflow SSH bounds are 1800, 1950 and 2100 seconds; the
production job has a 65-minute deadline covering artifact preparation and
transfer as well. The initial-install systemd sequence keeps its 120-second
aggregate and 30-second per-call caps, starting only after reconciliation
succeeds. A reconciliation timeout retains the candidate and receipt for
retry. The normal activation path owns the reviewed
health, Cloudflare and maintenance timer enablement, and installs the
off-site-backup unit/timer without silently enabling off-site backup before its
manual restore-drill gate. See the [read-only systemd capture-precondition probe](release-state-machine.md#read-only-systemd-capture-precondition-probe) before retrying a receipt-capture failure.

New shared venvs and pip code use a local `umask 022` subshell so API/worker
identities can traverse/read them; outer `umask 077` protects receipts, temp files and service envs.
Candidate cache cleanup permits service-owned entries only after
`legacy-services-restored`, fresh readiness and web `KillMode=control-group`;
dirfd/no-follow checks stay within that subtree. Other entries stay root-owned; unsafe state retains the receipt.

If candidate activation fails, the workflow retains the receipt and records
read-only filesystem, inode, mount and API sandbox facts, a sanitized systemd snapshot, and the last three minutes of API, worker and web journals.
If no complete `RELEASE_DEPLOY` marker is accepted, it emits
`RELEASE_REMOTE_DIAGNOSTIC` with stage `pre_ssh_dev_head`, `remote_dispatch` or `marker_validation`, and reason
`dev_head_probe_failed`, `dev_head_mismatch`, `remote_failed_without_marker` or `marker_missing_or_invalid`.
It includes only stdout/stderr byte counts. `ssh_rc` is the local timeout/SSH
status or `unavailable` before SSH; valid deployment markers remain authoritative.

Use `release-state-machine.md` for phase-specific recovery. An ERR/TERM/INT
after the snapshot may restore the old runtime and only services that were
active before quiesce, but it retains the migration receipt and never performs
an Alembic downgrade. Abort restores units and Nginx without an unconditional
restart, then checks only services that were active before quiesce. Intentionally
inactive services and timers remain stopped. A pointer, identity, restart or
readiness mismatch retains the receipt for another guarded attempt.

An interrupted pre-promotion install uses the signed immutable recovery workflow `.github/workflows/platform-production-release-recover.yml`. Ordinary `recover_pending` restores the exact v1/v2 service/timer snapshot only when the candidate is absent or empty. The workflow also has a default-off `source_bound_liveqa_reuse` option for the reviewed exact source-9ce artifact tuple. Enable it only when that original artifact and both required attestations are independently bound. The locked host proof can skip runtime reconciliation only when the existing `current` release and LiveQA payload match the archive-derived map; refusal or timeout stops recovery before the old installer. Leave the option off for ordinary recovery, and do not treat proof success as attribution of an earlier failure. For the protected activation-pending recovery path, the migration preauthorization reads `PLATFORM_DATABASE_URL` from the fixed API service-selected `/opt/oldsparky/platform/shared/env/api.env` using the pinned safe environment parser, no-follow file opens and stable root-owned `platform`-group mode-0640 file identity checks; it ignores an inherited `PLATFORM_DATABASE_URL` and never logs environment values. For future installs, generated bytecode caches are omitted only from configured `SOURCE_TREES` copying; `liveqa-runtime` copying and exact payload-manifest verification remain unchanged. This forward policy does not modify the old source-9ce repair path. After the recorded systemd state is restored, the runtime wrapper polls each snapshot-active API and web readiness endpoint (`http://127.0.0.1:8010/api/v1/health/ready` and `http://127.0.0.1:3000/`) for at most 30 attempts, using `curl --fail --max-time 5` and a one-second delay between failed attempts. This tolerates `Type=simple` start-to-listener delays without restarting services or weakening the HTTP success predicate. Exhaustion emits one final `readiness_internal` or `readiness_public` failure marker for the failed endpoint and retains the pending receipt for review. Any Cloudflare failed-oneshot exception remains a fixed branch inside the signed `recover_pending` helper: it is eligible only for the reviewed 9ce/f115 install/rollback receipt pair and invocation-bound `pending_operation` exit-75 evidence, with empty process/job state and a timer window beyond the recovery timeout. The helper must match its independently derived event ID to the wrapper's precomputed transaction/receipt/invocation binding before any write or action. It writes one root-owned no-overwrite event record containing a fresh attempt nonce before stopping only `deadlock-cloudflare-ips.timer`; it may issue one fixed `reset-failed deadlock-cloudflare-ips.service`, never starts the service, and relies on ordinary receipt restoration to rearm the timer. The EXIT path may compensate only against that record and the same attempt nonce. Any missing or changed binding aborts with receipts retained. This source correction does not establish why the original transaction remained pending and is not an instruction for manual systemctl operations.
Before provisioning or another release, inspect `.release-operation.json` separately from venv transaction sidecars: healthy services and an unchanged venv do not prove this receipt is absent; populated `quiesce-pending` requires the signed quarantine path below.
The separate `quarantine_populated_quiesce` capability requires the recovery workflow to validate the exact failed deploy run, attempt, workflow, `dev` ref, source SHA and failed job before deriving the candidate slug from that tuple.
Under the release lock, the host binds the pending-receipt digest and pointer identities, then moves the root-owned candidate with same-filesystem no-replace semantics into private quarantine.
The candidate remains untrusted evidence: recovery never executes or deletes it and does not claim signed-artifact equivalence.
After snapshot restoration/readiness verification, it consumes only the matching quiesce receipt; the inspect action supports a bound retry after that receipt is consumed. Mismatches retain receipts and candidate bytes.
Quarantine preserves storage use. A later, separate `prune_quarantined_candidate` recovery action may remove only the exact candidate whose original receipt is `receipt_consumed`; use the signed workflow and its exact failed-deployment tuple, never a caller-supplied slug or direct helper invocation. The action binds the immutable original receipt digest and F candidate identity to a new U cleanup run/bundle intent, checks current/previous releases and the protected LiveQA provider closure under both canonical locks, and requires no pending release sidecars/jobs plus stable service, Nginx and readiness checks. It stages the candidate by same-filesystem no-replace rename, rechecks its root-owned no-follow tree snapshot and mount boundaries, then deletes only that staged inode. The old quarantine receipt remains unchanged; a durable U-bound deleted intent makes exact replay a no-op, while an interrupted deletion resumes only that same staged inode. Any identity, provider, tree, pointer or health mismatch retains the remaining evidence for a later exact retry. See the [quarantine-pruning decision](adr/quarantined-candidate-pruning.md). Never invoke recovery helpers directly or normal deploy.

For code/runtime rollback after reviewing database compatibility, the guarded
abort restores recorded pointers/venv and never downgrades Alembic. Invoke only
the helper at the validated receipt's immutable `candidate_release` path after
verifying its recorded digest:

```bash
/opt/oldsparky/platform/releases/<candidate-slug>/tools/platform_release_deploy.sh \
  --abort-retained \
  --confirm-migration-not-reversed \
  --app-dir /opt/oldsparky/platform
```

It restores only services recorded active before quiesce; failures retain the
receipt. After pointer rollback never select helpers through `current/tools`
or the restored release. If the transaction-bound helper cannot be verified,
leave the receipt retained and use the documented immutable recovery entrypoint.

Rollback has a separate root-owned
`shared/.release-systemd-state.json` receipt. Before switching pointers it
records the exact active (`active|inactive`) and enablement
(`enabled|disabled|static`) state of every unit owned by the platform unit
installer. The transaction and systemd receipts share one immutable
`operation_id` and are validated together against both canonical release
paths/inode identities before any runtime helper, systemd operation or receipt
clear. The receipt also carries digest manifests for both rollback targets,
including the live-QA runtime installer; runtime restoration must name the
target whose manifest is being executed. It is retained on an installer,
restart, smoke or interruption failure.
Recovery restores only that closed owned set, first without `--now` enablement
and then to the recorded active state; an unsupported or malformed state fails
closed. `--no-restart` installs the files with activation disabled and verifies
the recorded active state without starting units. The receipt is removed only
after the rollback transaction has completed successfully. When a rollback
filesystem restore completes before its runtime restore, the immutable
transaction records `filesystem-restored-runtime-pending`; retries replay the
bound runtime/systemd restore before advancing to `recovery-restored`, so a
crash in that window cannot skip runtime repair. A legacy v2
`install`/`recovery-restored` receipt with no operation ID is recoverable only
when the systemd receipt is absent and the candidate is inactive; that bridge
performs receipt cleanup only and never executes retained release helpers. Runtime rollback uses each non-legacy target's immutable installer; only the exact M8 source SHA may use the transaction-bound current/candidate installer for rollback or install recovery while the original pointer topology matches. Normal candidate install, active-runtime validation and all other targets remain strict.

The legacy production release-abort workflow is only a compatibility bridge.
It accepts the exact operation-less v2 `install`/`recovery-restored` receipt,
requires a missing systemd receipt, inactive candidate and absent peer, and
uses the supplied installed recovery-generation SHA. It performs receipt-owned
cleanup only; it never parses an operation-ID receipt and never executes a
retained release's deploy, runtime or systemd helper. Operation-ID receipts use
the immutable recovery-bootstrap workflow below. A malformed, mismatched or
systemd-paired receipt remains retained.

## Immutable recovery bootstrap

Use the manual **Platform production recovery bootstrap abort** workflow only
after reviewing the retained receipt and confirming that the migration was
not reversed. Supply the exact successful security run ID/attempt, the exact
completed recovery-build producer run ID/attempt, and the exact completed
publisher run ID/attempt; then type `ABORT-RECOVERY-BOOTSTRAP-RETAINED-ONLY`.
The workflow validates those exact attempts, route artifact, schema-3 evidence,
bundle digest and build attestation before it reads `PROD_SSH_*` secrets or
opens SSH. The bundle source SHA **A**, producer workflow SHA **B**, and
publisher workflow SHA **C** are separate bindings: attestation uses **B**,
while the evidence and inner bundle name bind **A**, **P**, and **C**. The
publisher's outer artifact name includes its exact **C** run/attempt; its
positive API artifact ID and digest are carried in schema-3 evidence and are
checked against the downloaded outer ZIP. A rerun is
selected only by its supplied ID and attempt; a latest-by-SHA match is never
accepted. The downloaded bundle is re-hashed and re-stat'ed immediately before
transfer; publisher evidence also carries the producer artifact API digest,
which is checked before the publisher extracts the bundle member. It transfers
no source checkout to the host. GitHub's certificate
`runInvocationURI` is scoped to the run and attempt, not an individual job; the
workflow therefore binds the numeric producer and publisher job IDs selected
from their exact attempt jobs APIs into the closed evidence artifact and
rejects any evidence/API pairing drift before accepting the attestation.
After upload, publisher C reads the complete artifact inventory at most six times (two-second intervals; five-second request cap), waiting only for a valid complete page without the exact C artifact; HTTP/auth, malformed/incomplete, duplicate/expired, and name/run/SHA/digest failures reject immediately, while final absence fails closed before evidence or host changes. The release-recover transfer records the device/inode of its fresh root-owned mode-`0700` host staging directory and rechecks that identity and exact metadata before bundle acceptance and cleanup. Acceptance requires a root-owned mode-`0600`, single-link regular bundle with the attested digest; cleanup unlinks only the fixed `bundle.zip` path in that bound directory, then removes the directory only if empty.

The six operator inputs are the exact `security_run_id/security_run_attempt`, `recovery_run_id/recovery_run_attempt`, and `publisher_run_id/publisher_run_attempt` pairs. Attempt-job IDs and artifact digests are API-bound; missing, duplicate, expired or mismatched rows fail before SSH or host mutation.

The host installs verified bundles as immutable generations. Normal recovery invokes `platform_recover_pending.sh`; abort invokes `platform_abort_retained_only.sh`. For pre-managed-LiveQA targets missing old helpers, use the guarded fallback in the [recovery ADR](adr/recovery-bootstrap-retained-abort.md). Both retain unsafe receipts and never downgrade or run `systemctl` manually. The same workflow exposes `capture_maintenance_failure` and `reset_maintenance_failed`; confirmations are `CAPTURE-MAINTENANCE-FAILURE` and `RESET-MAINTENANCE-FAILED`. Both require the six exact run/attempt inputs. Capture also requires `recipient_certificate_b64`, one base64 PEM X.509 certificate with pinned DER SHA-256 `28d857f5fc6ff987444ca920a2bb5072081dbf2e41ef158d362b60e45e3f67a3`. The host retains raw evidence; Actions uploads only `evidence.cms` and `manifest.json`, never the private key or plaintext digest/size. A capture may upload an encrypted diagnostic while the workflow remains failed; runtime diagnostics include ordered bounded context for recovery receipt validation, LiveQA reconcile, unit/Nginx install and Nginx repair only inside ciphertext. After verifying the exact capture run, artifact ID/name and GitHub API archive SHA, create a mode-`0600` closed binding JSON under `.git` and decrypt the original ZIP with [the encryption ADR](adr/maintenance-failure-evidence-encryption.md): `python3 platform/tools/platform_decrypt_maintenance_evidence.py --artifact-archive <0600-archive-under-.git.zip> --binding-json <0600-api-binding-under-.git.json> --private-key <0600-key-under-.git> --certificate <matching-cert-under-.git> --destination <new-path-under-private-.git-dir> --git-dir <repo-.git>`. The tool checks the archive digest, exact members, manifest and independent bindings before writing a no-replace mode-`0600` plaintext file; the diagnostic supports cause review only and cannot authorize reset. Reset requires successful maintenance evidence, its locally reviewed plaintext SHA, matching capture/source and the same-source recovery bundle; it reads durable host evidence without transferring plaintext. Neither action starts maintenance or authorizes `--apply`.

For an operation-ID v2 `install` receipt from a pre-managed-LiveQA release, recovery requires no paired systemd receipt, exact transaction pointers/identities, and both old-release inputs absent:
`tools/platform_live_qa_runtime_install.py` and `liveqa-runtime/`. Partial payloads fail closed.
Immutable recovery prepares old unit/Nginx files without reconcile and restores the recorded service/timer state.
`legacy-services-restored` is written only after state checks and a bounded 30-second
API/web readiness wait; retries recheck readiness before cleanup. Shared QA remains untouched.
On `quiesce_recovery/systemd_state` failure, a paired closed `RECOVERY_CHECK` label identifies the failed guard without helper stderr; retain the receipt and do not retry or clear it manually.

The normal systemd installer uses the same crash discipline for retired unit
cleanup: it persists an fsynced, root-owned file/digest/state record before
the first stop or disable. After the new unit set and reload verify, it marks
that record `phase=cleanup-pending` before deleting any backup. If a process
dies during an unlink, directory removal, or final record removal, the next
retry validates only the identity-bound remaining paths and converges cleanup;
it never adopts an unknown backup or clears an unsafe record.

For an interrupted first install with no `current`, release recovery selects
the exact root-owned, immutable recovery generation bound to the supplied
security run/attempt and recovery build run/attempt/job evidence. If that
content address or capability is unavailable, recovery fails closed; it never
scans for a latest or sole generation and never resolves helpers from a missing
current release. A staged operation-ID receipt requires its complete,
transaction-bound `systemd_state_before` snapshot. Recovery validates the
receipt and restores/verifies that baseline before candidate or transaction
cleanup; a stale `.release-systemd-state.json`, missing snapshot or incomplete
pair is retained without systemd mutation. Failures are retryable and never
delete the candidate before the baseline proof. The separate operation-less
pre-promotion v1/v2 compatibility bridge has no operation ID and no systemd
receipt; only that bridge performs the documented zero-systemd no-op for an
all-inactive/disabled first-install snapshot. A deliberate current-only
topology has no first-install systemd receipt, but its immutable receipt must
contain the complete pre-quiesce API/worker/web/timer snapshot; recovery
restores those exact states before cleanup and retries from a durable
`filesystem-restored-services-pending` phase after an interruption. Partial or
missing current-only snapshots remain retained before any systemd call.

Draft publication and public convergence are separate checks: a successful Worker publish does not prove that every edge serves the new asset. The build job passes the upload actions' artifact IDs and binds the bundle manifest to the target SHA and build run/attempt; the release job consumes those IDs instead of reconstructing names from its own attempt, which can differ after a rerun. Smoke polls a bounded number of times for HTTP 200 and the exact expected body hash, recording response-specific cache/edge headers on mismatch; a `HIT` header alone does not establish stale content. If artifact lookup fails before any Cloudflare write, verify the source and absence of an active run, then use one canonical fresh dispatch to rebuild and publish; do not rerun failed jobs against attempt-derived names or substitute artifacts.

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

Remote production deploy/preflight also requires a complete structured
`RELEASE_DEPLOY schema=1` marker. A zero SSH exit without that marker is a
transport failure and cannot be reported as a successful deployment.

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

If an operation-ID rollback is interrupted, use the exact content-addressed
generation selected and verified by the release-recover workflow. Invoke its
immutable wrapper directly; do not use `current/tools` or a release rollback
shim as recovery control code:

```bash
/opt/oldsparky/platform/shared/.release-recovery/generations/<bundle-sha256>/platform_recover_pending.sh \
  --app-dir /opt/oldsparky/platform
```

The wrapper resumes the recorded rollback phase, validates the operation-ID
and systemd receipt pair before systemd or runtime work, and completes receipt
cleanup in two phases. A crash after receipt clear is finished from the
transaction phase without querying systemd. The legacy shim remains only for
normal rollback handoff compatibility and is not an operation-ID recovery
authority.

After rollback, repeat preflight plus origin/SNI and public smoke against the restored release.

A successful `Platform security and build` run for a push to the current `dev` HEAD continues automatically into production deployment; ignore stale successes.
Failed or missing gates stop the chain before production side effects.

## Special release contours

Open only when applicable:

- CSP candidate/enforcement changes, live browser QA, Turnstile/auth contour, AppArmor Chromium sandbox and CSP observation gates: [`csp-live-qa-runbook.md`](csp-live-qa-runbook.md).
- Live public browser workflows follow [`test-suite-governance.md`](test-suite-governance.md). Use the dedicated server wrapper over SSH; never run production Playwright on GitHub.
  CI hermetic and live-QA builders share pinned browser archives; update mirrors only after exact size/SHA-256 match, preserving redirect/hash checks.
- Backup/restore: [`backup-restore-runbook.md`](backup-restore-runbook.md).
- Security policy and CSP ownership: [`security-runbook.md`](security-runbook.md).
- Incident response: [`incident-response.md`](incident-response.md).
