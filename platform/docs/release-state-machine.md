# Release state machine and recovery contract

- Status: Active release design
- Owner: Production operator and platform maintainers
- Last reviewed: 2026-09-29

This document owns the end-to-end release transaction. The normal production
path is `tools/platform_release_deploy.sh`; the low-level installer is a
filesystem primitive and must not be called directly by CI or an operator.

## State flow

```text
canonical release lock `/run/lock/oldsparky-platform-release.lock`
 (held for the whole operation)
    -> pre-quiesce transaction receipt (.release-operation.json, quiesce-pending)
    -> quiesce writers
candidate artifact
    -> staged
    -> first-install systemd baseline captured (initial installs only)
    -> migration-pending
    -> migration-applied
    -> activation-pending
    -> services-restarted
    -> nginx-pending
    -> nginx-applied
    -> smoke-passed
    -> systemd-activation-pending (initial install only)
    -> systemd-activated (initial install only)
    -> activation-committed
```

The deploy wrapper acquires the release-independent lock before preflight and
holds that one lock through pre-quiesce receipt creation, quiesce, staging,
migration, activation and abort/recovery. The low-level installer reuses an
inherited release-lock file descriptor when called by the wrapper; direct
first-install,
rollback, runtime-restore and recovery invocations acquire the same lock
themselves, so they cannot overlap. The lock file is created and validated
under `/run/lock` before a first bootstrap creates `APP_DIR/releases/shared`.

Production deploy has one additional lock edge: it acquires the release lock
first, then `/run/lock/oldsparky-retained-load-matrix.lock`, and holds both
through candidate activation, runtime-profile mutation, service
restart/readiness, final smoke and the success boundary. Retained-load and
cleanup acquire only the second lock; storage maintenance acquires the release
lock and then the second lock; rollback, release recovery and service recovery
acquire only the release lock. No path acquires these locks in the reverse
order, so deploy cannot deadlock with a retained-load, maintenance or recovery
operation. Recovery passes the inherited release-lock file descriptor through
rollback and runtime restore and keeps it held for the final Nginx/readiness checks.

## Installation topology

Preflight is explicit about pointer topology. A clean first install has no
`current` or `previous` pointer and performs only layout, lock, shared-runtime
and candidate checks; it never invokes an old-release runtime. Once the staged
operation receipt exists, the wrapper captures the exact inactive and disabled
state of the seven platform-owned application units/timers into that same
receipt before any first-install systemd mutation. The receipt is the
authority for first-install activation and every abort/recovery retry. A
current-only install validates `current` and deliberately permits no
`previous`; it requires a complete API/worker/web/timer snapshot, permits no
first-install systemd receipt, and uses the immutable transaction helper to
restore that snapshot before cleanup. An upgrade requires both canonical
pointers and uses the full runtime/systemd snapshot contract. A previous
pointer without current, or any pointer/symlink identity mismatch, is
rejected. Rollback remains a separate two-pointer operation.

### Initial-install systemd boundary

The initial install keeps unit-file preparation separate from persistent
activation. During `activation-pending`, the installer runs with
`PLATFORM_ENABLE_SYSTEMD_UNITS=0`; services are restarted and readiness/smoke
checks run while the receipt remains durable. After smoke, the wrapper records
`systemd-activation-pending`, restores the captured baseline, and runs the
installer with `PLATFORM_ENABLE_SYSTEMD_UNITS=1`. It then verifies all seven
units are enabled, the three services and four timers are active, and both
readiness endpoints pass before recording `systemd-activated`.

The live-QA payload reconciliation is a separate, bounded candidate step.
The production supervisor supplies its monotonic candidate deadline; the
reconcile timeout is capped at 600 seconds and clamped to leave 600 seconds
plus timeout-reaping grace for activation, readiness, smoke and durable
receipt work. The candidate supervisor allows 1800 seconds, the remote
dispatcher 1950 seconds, and the workflow SSH call 2100 seconds. A reconcile
timeout or non-zero result leaves the candidate and receipt retained. For a
clean initial install, the existing 120-second systemd-operation deadline
starts only after reconciliation succeeds; individual systemd calls remain
capped at 30 seconds. Failures after `systemd-activated` or
`activation-committed` first restore and verify the captured baseline, then
rewind to `systemd-activation-pending` so the next resume does not require
units that recovery has already stopped. A stale or incomplete legacy
`.release-systemd-state.json` alongside an initial transaction is rejected
before cleanup; only the transaction-bound `systemd_state_before` snapshot is
accepted.

Recovery provenance is a three-way identity chain. The security
checkout/source SHA is **A**; the immutable producer workflow code SHA is
**B**; and the completed publisher workflow code SHA is **C**. The producer
and publisher are separate `workflow_run` jobs: the publisher accepts only the
supplied completed producer run/attempt and exact producer job/artifact, never
a latest-by-SHA match. Manual abort/recover inputs carry the exact security,
producer and publisher run/attempt pairs. Attestation verifies **B**, while the
bundle manifest/evidence independently bind **A**, producer **P**, and
publisher **C**. A branch advance or rerun therefore fails closed unless the
operator selects its exact identity.

Before stopping a writer or invoking the installer, the wrapper atomically
writes `shared/.release-operation.json` in the `quiesce-pending` phase. It
contains the exact original current and previous release identities, candidate
path and the API/worker/web/timer states. This pre-stage transaction is the
recovery authority if SIGKILL occurs before the installer can promote it to the
full staged schema. The installer promotes the same file while retaining the
snapshot before migration; the operation state file is removed only after
final commit.
In production the migration wrapper accepts only the exact two-argument
`upgrade head` command; introspection, downgrade, flags and other Alembic
argv forms are rejected before any writer quiesce or database access.

During a deploy, operational preflight remains mandatory while active-release
Alembic current/head equality is deferred until the authenticated candidate is
staged. With both release locks held and application writers stopped, the
candidate runtime validates that the single live `platformdb` revision is an
ancestor or the sole head of the candidate graph before recovery helpers or
migration writes run. Only a clean first-install transaction may have an
empty revision registry. Operator preflight and post-activation checks remain
strict; see the [candidate-bound migration guard ADR](adr/candidate-forward-migration-guard.md).

Before the first service stop, the deploy wrapper captures the state of
`deadlock-api`, `deadlock-worker`, `deadlock-web` and the
`deadlock-cloudflare-ips.timer`. It records that snapshot in
`shared/.release-operation.json` in `quiesce-pending` before quiesce or staging,
then promotes and validates it before entering `migration-pending`; the
receipt also identifies the exact application services quiesced by the
migration boundary. A production transaction without this snapshot cannot run
Alembic or restart services during recovery. If staging is interrupted, the
pre-quiesce transaction is sufficient to restore the old runtime and remove a
partial candidate, or remains retained for explicit abort when any identity,
pointer, restart or readiness check fails.

Pointer promotion is durable in two steps: after `previous` is switched the
transaction records `previous-switched`; after `current` is switched it
records `current-switched`, then `pointers-switched` and
`activation-pending`. A crash between either symlink update is therefore
recovered from the phase-specific topology rather than inferred from the live
links. In the narrow interval before `current-switched` is persisted,
`previous-switched` authorizes exactly either the previous-only pair or the
fully promoted pair; the immutable validator rejects every other combination.
Mutable systemd calls are individually bounded so a wedged manager leaves this
durable phase available for retry.

The immutable recovery wrapper has one deliberately narrow operation-less
exception for this boundary: an exact version-1 `install` receipt in
`quiesce-pending`, with complete active/inactive API/worker/web/timer state,
unchanged pointer identities, no populated candidate path and no systemd
receipt. New version-2 receipts additionally carry the exact enabled/disabled
state; version 1 has no enabled fields and never infers them. An empty,
canonical root-owned candidate directory is also safe to remove. It restores
that recorded service/timer snapshot through the generation's transaction
helper, then performs `abort-quiesce`. A malformed receipt, an unexpected
phase, an occupied or replaced candidate, or any partial snapshot remains
retained before
the first systemd call. This pre-promotion branch is not the legacy
`recovery-restored` cleanup bridge.

For a no-current, operation-less pre-promotion topology, a complete
all-inactive snapshot is accepted only as compatibility evidence: version 2
must also be all-disabled, while version 1 has no enablement fields. That
specific bridge is a filesystem-only no-op and never calls `systemctl`. It is
distinct from a staged operation-ID first install, whose durable
`systemd_state_before` snapshot is required for activation and recovery.

Rollback uses the same receipt discipline after switching pointers:

```text
pointer/venv switch
    -> rollback-runtime-pending
    -> filesystem-restored-runtime-pending
    -> immutable runtime/systemd restore
    -> rollback-cache-purged
    -> services-restarted
    -> smoke-passed
    -> rollback-runtime-applied
```

The rollback target's systemd units and Nginx configuration are installed while
the receipt is in `rollback-runtime-pending`, before any restart or smoke.
Before the restored release can restart, the transaction purges the fixed
legacy profile-access cache namespaces and records `rollback-cache-purged`.
That phase is the durable proof that the old runtime cannot reuse stale v1
authorization projections. Recovery resumes restart only from this phase; an
older `restart-pending` receipt lacks purge proof and fails closed with its receipts
retained. A purge failure leaves services stopped and recovery restores the
pre-rollback runtime instead of activating the old release.
`--no-restart` still restores units and Nginx but deliberately omits service
restart and smoke. Runtime restore always invokes the unit installer with
`PLATFORM_ENABLE_SYSTEMD_UNITS=0`, so restoring unit files never enables or
starts a timer implicitly.

Before the rollback pointer switch, the tool creates the separate root-owned
`shared/.release-systemd-state.json` receipt. It contains the exact active and
enablement state of the closed platform-owned service/timer set and the
pre-rollback release identities. Both receipts carry one immutable operation ID;
the systemd receipt also carries digest manifests for the helpers of both
rollback targets, including the runtime installer and recovery orchestrator.
Recovery validates IDs, paths, inode identities and the manifest for the
explicit target before any helper or systemd action, restores enablement
without `--now`, then restores active state only for the recorded units.
Unsupported or malformed state is fail-closed. Completion is two-phase:
`complete --retain-receipt`, systemd clear, then final `complete`; a crash
after clear leaves a retryable transaction and no guessed service transition.

Before a rollback pointer switch, the rollback tool refreshes the root-owned
`shared/.release-recovery/` bundle and installs a small compatibility shim as
the previous release's `tools/platform_release_rollback.sh`. That shim is a
normal rollback handoff only. Operation-ID recovery is entered through the
exact verified content-addressed generation and its immutable
`platform_recover_pending.sh` wrapper; it never selects `current/tools` or the
shim as control code. The application files and runtime tools of the previous
release remain unchanged; only its rollback entrypoint is replaced by the
compatibility handoff needed for the normal cross-release boundary.

## Failure behavior

- A failure before `staged` is recovered from the pre-quiesce transaction by
  the transaction tool. The original
  pointers and shared venv remain authoritative; a partial candidate is
  removed only after its path and ownership are validated.
- `migration-pending` and `migration-failed` retain the transaction because
  the Alembic outcome may be uncertain. An ERR/TERM/INT handler may restore
  the old runtime and only units that were active before quiesce when the
  original pointers and release identities still match, but it never downgrades
  the database and never clears the receipt. An operator must check the
  database and either resume with `platform_release_deploy.sh --resume` or
  perform an explicit compatibility review before code/runtime rollback. The
  explicit abort path is:

  ```bash
  tools/platform_release_deploy.sh \
    --abort-retained \
    --confirm-migration-not-reversed \
    --app-dir /opt/oldsparky/platform
  ```

  This restores the recorded pointers and venv, reapplies the old units and
  Nginx configuration without an unconditional restart, then idempotently
  purges the fixed legacy profile-access cache namespaces before reactivating
  the prior runtime, then restarts and checks only services that were active
  before the transaction. If the source-bound purge cannot complete, recovery
  keeps the application services stopped and retains the transaction.
  Services intentionally inactive before quiesce remain stopped. A restart,
  readiness, pointer or identity mismatch retains the receipt for another
  recovery attempt. The confirmation is an operator statement that the
  database migration was not reversed and compatibility has been reviewed.
- `migration-applied` and every later phase retain the candidate and state on
  failure. Do not delete the state file, downgrade Alembic, or run a second
  unrelated install. Resume first; rollback remains a code/runtime operation
  and never reverses database migrations automatically.
- A clean first-install failure in `activation-pending` or any later phase
  retains the candidate and operation receipt. The failure handler restores
  and verifies the receipt-bound seven-unit baseline before returning. A
  failure after a durable activation marker rewinds the marker to
  `systemd-activation-pending`; retry is therefore idempotent and never
  assumes that stopped units are still active. Abort at `staged` or
  `recovery-restored` proves the exact baseline before completing filesystem
  cleanup. If that proof, its timeout budget, or the receipt pair fails, no
  candidate or receipt is removed.
  A service-owned web cache inside an inactive candidate is the sole ownership
  exception: cleanup requires the verified `legacy-services-restored` phase,
  API/web readiness and loaded web-unit `KillMode=control-group`; all other
  candidate entries remain root-owned.
- `nginx-pending` is an explicit uncertainty boundary. If the process stops
  after Nginx has been mutated but before `nginx-applied`, abort recovery first
  restores the recorded pointers/venv and then reinstalls the previous
  release's units and Nginx configuration before restart/readiness/smoke.
- A rollback runtime failure retains `rollback-runtime-pending` (or its later
  phase). Recovery first durably restores the filesystem and venv, recording
  `filesystem-restored-runtime-pending` before invoking any runtime or systemd
  helper. A retry at that marker replays the bound runtime/systemd restore and
  only then advances to `recovery-restored` and the two-phase receipt cleanup.
  Recovery resumes an already committed `rollback-cache-purged` operation and
  refuses a legacy `restart-pending` receipt without purge proof; otherwise it
  restores the exact pre-rollback pointers, venv, units and Nginx while both
  the rollback transaction and the systemd-state receipt remain durable.
  Recovery is invoked through the shared bundle, including when `current`
  already resolves to the previous release. A missing, stale or malformed
  systemd-state receipt never authorizes an enable/start operation.
- An operation-ID first install with both pointers absent is recovered only
  with its transaction-bound `systemd_state_before` snapshot; immutable
  recovery restores and verifies that baseline before candidate/receipt
  cleanup. A stale separate `.release-systemd-state.json`, missing snapshot,
  or incomplete pair fails closed. The operation-less pre-promotion bridge is
  the separate systemd-free compatibility path described above. A current-only
  receipt must instead carry the complete pre-quiesce service/timer snapshot;
  immutable recovery restores it before candidate and receipt cleanup, with a
  durable retry marker if restoration is interrupted.
- A legacy v2 install receipt in `recovery-restored` has no operation ID and is
  not upgraded in place. Only the immutable recovery-bootstrap bridge may
  consume it, and only when the systemd receipt is absent, the candidate is
  inactive and the peer is absent; that path performs transaction cleanup only
  and never executes retained release helpers. The normal release-recover
  workflow rejects it and directs the operator to that bridge.
- `activation-committed` is resumable and idempotently calls final receipt
  completion. A crash after activation commit therefore cannot report success
  while leaving the receipt to block the next install.

The same explicit abort path may recover a `staged` receipt when staging has
already quiesced services. It still requires the confirmation flag for a
single guarded operator command, but no migration authorization is applied to
that phase. A SIGKILL before promotion is represented by
`.release-operation.json` with `phase=quiesce-pending`; `--abort-retained`
consumes that receipt, restores its exact service state and removes only an
empty, canonical candidate directory. A populated or replaced candidate is
retained with the receipt. Missing or malformed
service-state data is never interpreted as “all active”; recovery stops before
any service start and retains the receipt.

The retained state is the recovery receipt: the pre-quiesce transaction phase
records the original pointers, candidate path and service snapshot, while the
promoted operation receipt additionally records candidate and shared-venv
identities.
A restart/readiness or smoke failure therefore cannot silently leave an
untracked pointer/venv combination. Recovery also rejects a pointer pair
outside the transaction's recorded pre-switch or in-transaction transition
states; it never starts services after an unrelated pointer or release
identity is detected. Receipts from an older schema are not guessed or
rewritten: production migration and recovery fail closed and require the
documented recovery bundle/operator review.

## Operator command

```bash
cd /opt/oldsparky/platform/current
tools/platform_release_deploy.sh \
  --artifact /path/to/<release-slug>.tar.gz \
  --app-dir /opt/oldsparky/platform \
  --expected-csp-mode enforce
```

If the command reports a retained transaction, inspect the phase and database
revision, then resume only the same transaction:

```bash
tools/platform_release_deploy.sh --resume --app-dir /opt/oldsparky/platform
```

For an install receipt in a non-migration-uncertain phase such as
`nginx-applied`, an operator may perform the explicit recovery through GitHub
Actions after reviewing the receipt:

```bash
gh workflow run platform-production-release-recover.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f confirmation=RECOVER-PENDING-RELEASE \
  -f security_run_id=<exact-security-run-id> \
  -f security_run_attempt=<exact-security-run-attempt>
```

The recovery restores the exact pre-operation release/runtime and verifies
service readiness before the normal deployment chain is retried. It must not
be used for `migration-pending` or `migration-failed` without the separate
database compatibility review and explicit abort/resume decision.

When the receipt is in `migration-applied` or a later phase and the reviewed
database evidence confirms that the migration was not reversed, use the
immutable recovery-bootstrap abort workflow described in the runbook. The
older release-abort workflow is compatibility-only: it accepts only a legacy
operation-less v2 `install`/`recovery-restored` receipt with no systemd receipt
and performs receipt cleanup through the installed immutable generation. It
never invokes a retained release deploy/runtime helper.

```bash
gh workflow run platform-production-release-abort.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f confirmation=ABORT-LEGACY-RELEASE \
  -f generation_sha=<installed-recovery-generation-sha256>
```

The legacy bridge performs receipt-owned candidate/venv cleanup only through
the installed immutable generation. It does not restore runtime or Nginx,
restart or verify services, invoke a retained release helper, or downgrade
Alembic. It accepts only an operation-less v2 `install`/`recovery-restored`
receipt with no systemd receipt and leaves malformed, mismatched, or
operation-ID receipts retained for the immutable recovery-bootstrap workflow.

The immutable recovery-bootstrap workflow also has a narrow operation-ID
`install`/`recovery-restored` path for an original release that predates
managed LiveQA. It is accepted only with no paired systemd receipt, verified
transaction identities and pointers, and both legacy-release LiveQA inputs
absent. The recovery generation prepares the old unit/Nginx files without
running reconcile, restores the recorded service/timer snapshot, verifies
readiness, and persists `legacy-services-restored` before receipt cleanup.
Partial LiveQA payloads fail closed. Generic phase changes and generic
recovery cannot create or rewind that proof, and retries recheck readiness
before cleanup; the shared managed-LiveQA state is left untouched.

The deploy gate also requires a read-only Cloudflare/Nginx/UFW range-parity
proof and a direct-origin negative test. The current closure evidence for the
production source SHA `97db79b681dd90cc8e89dd91f549610c943c16b8` is recorded in
[`archive/as-12-origin-perimeter-2026-09-05.md`](archive/as-12-origin-perimeter-2026-09-05.md).
Any future perimeter, listener or trust-configuration change requires a fresh
proof run before the affected release is approved.

## Explicit non-goals

- Alembic downgrade is not part of deploy recovery.
- A production browser/translation warm-up is not read-only QA. The patch
  translation workflow is now named and reported as a controlled warm-up with
  an explicit cache-miss/OpenAI call budget.
- Building on the production host from a source archive is not immutable
  provenance and is not part of the normal workflow. CI now builds and attests
  the immutable artifact and wheelhouse; the VPS verifies the artifact digest
  and source commit before installation. A reviewed push to `dev` is now the
  normal path: successful exact-SHA security/build feeds the automatic
  production chain. Manual production dispatch remains an operator fallback.
