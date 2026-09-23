# Release state machine and recovery contract

- Status: Active release design
- Owner: Production operator and platform maintainers
- Last reviewed: 2026-09-12

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
    -> migration-pending
    -> migration-applied
    -> activation-pending
    -> services-restarted
    -> nginx-pending
    -> nginx-applied
    -> smoke-passed
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

Rollback uses the same receipt discipline after switching pointers:

```text
pointer/venv switch
    -> rollback-runtime-pending
    -> restart-pending
    -> services-restarted
    -> smoke-passed
    -> rollback-runtime-applied
```

The rollback target's systemd units and Nginx configuration are installed while
the receipt is in `rollback-runtime-pending`, before any restart or smoke.
`--no-restart` still restores units and Nginx but deliberately omits service
restart and smoke. Runtime restore always invokes the unit installer with
`PLATFORM_ENABLE_SYSTEMD_UNITS=0`, so restoring unit files never enables or
starts a timer implicitly.

Before the rollback pointer switch, the tool creates the separate root-owned
`shared/.release-systemd-state.json` receipt. It contains the exact active and
enablement state of the closed platform-owned service/timer set and the
pre-rollback release identities. Recovery validates this receipt before any
systemd action, restores enablement without `--now`, then restores active state
only for the recorded units. Unsupported or malformed state is fail-closed;
the receipt remains available when installation, restart, smoke or recovery is
interrupted and is removed only after the rollback transaction completes.

Before a rollback pointer switch, the rollback tool refreshes the root-owned
`shared/.release-recovery/` bundle and installs a small compatibility shim as
the previous release's `tools/platform_release_rollback.sh`. If the rollback
process crashes after `current` has switched, a new invocation through the old
`current` therefore delegates to the shared bundle rather than the old
release's transaction code. The application files and runtime tools of the
previous release remain unchanged; only its rollback entrypoint is replaced by
the recovery handoff needed for this cross-release boundary.

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
  restarts and checks only services that were active before the transaction.
  Services intentionally inactive before quiesce remain stopped. A restart,
  readiness, pointer or identity mismatch retains the receipt for another
  recovery attempt. The confirmation is an operator statement that the
  database migration was not reversed and compatibility has been reviewed.
- `migration-applied` and every later phase retain the candidate and state on
  failure. Do not delete the state file, downgrade Alembic, or run a second
  unrelated install. Resume first; rollback remains a code/runtime operation
  and never reverses database migrations automatically.
- `nginx-pending` is an explicit uncertainty boundary. If the process stops
  after Nginx has been mutated but before `nginx-applied`, abort recovery first
  restores the recorded pointers/venv and then reinstalls the previous
  release's units and Nginx configuration before restart/readiness/smoke.
- A rollback runtime failure retains `rollback-runtime-pending` (or its later
  phase). Recovery either completes the already committed restart-pending
  rollback or restores the exact pre-rollback pointers, venv, units and Nginx
  while both the rollback transaction and the systemd-state receipt remain
  durable. Recovery is invoked through the shared bundle, including when
  `current` already resolves to the previous release. A missing, stale or
  malformed systemd-state receipt never authorizes an enable/start operation.
- `activation-committed` is resumable and idempotently calls final receipt
  completion. A crash after activation commit therefore cannot report success
  while leaving the receipt to block the next install.

The same explicit abort path may recover a `staged` receipt when staging has
already quiesced services. It still requires the confirmation flag for a
single guarded operator command, but no migration authorization is applied to
that phase. A SIGKILL before promotion is represented by
`.release-operation.json` with `phase=quiesce-pending`; `--abort-retained`
consumes that receipt, restores its exact service state and removes a partial
candidate. Missing or malformed
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
  -f confirmation=RECOVER-PENDING-RELEASE
```

The recovery restores the exact pre-operation release/runtime and verifies
service readiness before the normal deployment chain is retried. It must not
be used for `migration-pending` or `migration-failed` without the separate
database compatibility review and explicit abort/resume decision.

When the receipt is in `migration-applied` or a later phase and the reviewed
database evidence confirms that the migration was not reversed, use the
explicit retained-release abort workflow:

```bash
gh workflow run platform-production-release-abort.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f confirmation=ABORT-RETAINED-RELEASE-MIGRATION-NOT-REVERSED
```

This restores the exact pre-operation release/runtime, restarts and verifies
only services recorded active before quiesce, leaves intentionally inactive
units stopped, and never downgrades Alembic.

The abort workflow independently validates the v2 receipt (or the exact
pre-quiesce receipt schema) and the release identity before selecting the
transaction/deploy tools. It then verifies that recovery consumed the receipt,
restored both pointers, and left API, worker, web, and the Cloudflare timer in
the exact durable active/inactive states recorded before quiesce. Readiness is
required only for units recorded active; a missing, stale, legacy, or
identity-mismatched receipt/tool fails closed and remains available for
operator recovery.

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
