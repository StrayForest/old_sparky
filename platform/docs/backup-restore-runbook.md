# Platform backup and restore runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-10-12

## Local verified backup

`platform_backup_restore_drill.py` is the only DB-backup owner. It dumps
`platformdb.platform` plus `public.alembic_version`, writes a private checksum
manifest, and verifies the dump by restoring it into a disposable PostgreSQL 16
cluster. The cluster runs in a private 512 MiB tmpfs inside a transient
systemd unit capped at 640 MiB memory with swap disabled. It has no network
access, cannot see production PostgreSQL data/socket paths or shared
credentials, and runs `pg_restore` as the `postgres` OS user. The root-owned
archive is opened and hash-pinned, then exposed read-only; `pg_restore` reads
that seekable file descriptor from standard input and restores tablespaces
inside the private cluster. SQL restore and validation use a passwordless
`platform_user` role created only in that disposable cluster; its separate
`postgres` admin connection creates and drops the temporary database.
Production PostgreSQL is never used as the restore target. The disposable
cluster is stopped and its unit and mount are confirmed gone before a verified
backup pair is published. Ordinary rotating maintenance retains 14 verified
copies; backup-only preserve-existing mode keeps every complete pair.

The lock-aware storage-maintenance path calls backup creation and freshness
verification in-process, avoiding a second Python interpreter during the
memory-intensive restore drill. The standalone helper CLI remains available
for local diagnostics and checks. Do not run its mutating mode directly on the
production host because it does not acquire the host-wide operation locks.
Create a production backup through the lock-aware backup-only mode:

```bash
cd /opt/oldsparky/platform/current
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_storage_maintenance.py \
  --app-dir /opt/oldsparky/platform \
  --backup-max-age-hours 24 \
  --backup-only --apply --json
```

The drill checks disk availability before starting `pg_dump` and monitors the
root filesystem while dump and restore children run. Its stop threshold is the
larger of 5 GiB or 15% of filesystem capacity, with a 256 MiB lead margin.
During isolated restore it also checks that the effective tmpfs remains exactly
512 MiB with at least 64 MiB free, the unit stays below its 640 MiB memory cap
with at least 32 MiB headroom, swap remains disabled, and host `MemAvailable`
stays at or above 1 GiB. It stops and reaps only the owned transient unit on a
failed or unavailable sample. The monitor is sampled, so these margins are
early-stop bounds rather than guarantees against a single allocation burst.
A below-threshold host-memory admission failure retains the sampled
`MemAvailable` value and best-effort bounded RSS samples for the helper and
its direct parent in the root-private failure receipt. Unavailable process
samples remain null; this telemetry never changes the 1 GiB admission guard
and is not included in the public workflow output or artifact.
A failed or unavailable disk, memory or mount sample stops the drill; it does
not rotate or remove any retained backup. A failed isolated restore or
preserve-existing post-publication check removes only the operation's
inode-pinned new pair and leaves the existing backup inventory unchanged. If
exact rollback cannot be confirmed, the operation reports a closed
backup-integrity status and does not claim preservation.

The transient restore unit must reach an active state with its process and
control group verified within 30 seconds. While the launcher is running,
`loaded`/`not-found` units may remain `inactive` only without a process
identity; an `activating` unit may expose a preliminary identity, which is not
accepted until the active-state checks complete. A failed unit result or
launcher exit fails closed. If
confinement verification fails after the unit starts, the private diagnostic
records only closed error and errno fields plus bounded output hashes; raw
launcher output stays in the root-only workflow capture.
The worker waits on its stdin pipe before inspecting the scratch mount, archive,
resource state or PostgreSQL. The parent releases the fixed `START` token only
after two consecutive checks bind the same active unit invocation, PID, process
start time, cgroup, pinned interpreter inode and cgroup limits. After restore,
Alembic and table checks, the worker flushes one bounded result frame and waits
for `DONE` without touching scratch resources. The parent accepts only that
complete strict frame, repeats the full live identity and resource checks, then
sends `DONE` under the existing runtime deadline. It requires the launcher to
exit successfully and the final stdout bytes to match the authorized frame
exactly. Any identity change, malformed/extra output, missing token, early
launcher exit or timeout stops and reaps the exact unit without accepting a
result. The private schema-2 diagnostic may include a closed lifecycle-only
snapshot taken before unit stop; it preserves the original failure snapshot
and never affects success classification. In schema 2, the START/DONE token
booleans mean the complete fixed token was written to the private pipe; they
do not mean the worker received or accepted it. `false` means a complete write
was not confirmed and does not assert that no prefix byte arrived. A late
complete write is recorded truthfully but still fails the deadline and cannot
authorize result acceptance. Existing schema-1 diagnostics remain valid under
their original closed schema.
If the unit reaches verified confinement and the restore command then exits
unsuccessfully, the failure report records a separate closed
`unit_exited_after_confinement` phase with the final unit state and bounded
output hashes. This remains a failed restore; the diagnostic does not change
the admission limits, confinement checks or backup publication rules.

Keep this backup budget separate from the release installer budget. The
release coexistence reserve covers release staging and is not backup capacity.
Backup creation writes a temporary dump on the backup filesystem. Unless
dump-only mode is explicitly selected by an approved workflow, it restores the
dump in the bounded tmpfs cluster before publishing the archive and metadata.
The restore does not allocate a second production database or copy the archive
into tmpfs. The tmpfs and systemd cgroup are jointly bounded; if either limit
or the host-memory reserve is unavailable, verification fails without
publishing a pair.

After `pg_dump` succeeds, the drill opens the temporary dump without following
symlinks, verifies its recorded inode and effective-UID-owned mode-`0600`
regular-file identity (root in production), and fsyncs that file before
restore admission or publication. It rechecks the open file and path identity
after the sync. This also applies to approved dump-only creation. An open,
identity or fsync failure aborts the operation; cleanup removes only the exact
unpublished temporary inode. The later backup-directory fsync protects the
published names and does not replace the dump-file sync.

After publishing both the dump and metadata entries, the drill fsyncs the
backup directory before rotation or success. If that directory sync fails, it
removes only the exact newly published inodes and leaves pre-existing pairs
untouched; failure to confirm that rollback is reported as an integrity error.

Backup-only preserves every complete archive/metadata pair that exists before
the run; it does not apply the 14-copy rotation limit. Under the ordered
maintenance locks, it validates that each sidecar names the matching dump and
records its exact size and SHA-256. The preservation inventory hashes both
files and binds their paths, inode identities, modes, link counts, sizes and
modification times. The operation checks that inventory before publishing and
afterward, confirms exactly one new complete pair, and fails closed on an
orphan, mismatch or change. It never deletes a pre-existing pair. Use full
storage maintenance with
`--backup-keep 14` when the reviewed maintenance policy calls for bounded
rotation. The low-level backup CLI retains its historical rotation default;
the lock-aware backup-only caller explicitly selects preservation.

For a reviewed `dev` release, an operator may run the same guarded backup
through GitHub Actions without opening a direct production shell. The backup
workflow requires the exact `Platform security and build` run ID, attempt, and
source SHA for the current `dev` head. Dispatch it while that exact CI run is
still running, after confirming that no production deployment currently owns
the shared production workflow slot. The backup workflow waits on that one run
for at most 30 minutes of monotonic wall time within its 120-minute job
deadline. The attestation verifier is capped at 10 minutes; before SSH starts,
the workflow requires at least 60 minutes to remain, allowing a 45-minute
backup/restore budget plus 15 minutes for completion and cleanup. Each API
request and sleep is capped by the remaining poll deadline. It never substitutes another run or a newer `dev`
head. It validates the exact successful verification job, artifact digest,
attestation, and seven-file helper manifest before staging anything on the host.
It runs those staged, source-bound helper bytes for both create and
verify-existing. If the run fails, times out, the source moves, or any bundle
check fails, it stops without running a production backup or falling back to
the installed helper. The normal deploy's freshness check is unchanged.

After staging, an `always()` finalizer removes any leftover run-specific helper
bundle before deleting the SSH material. Transfer, the remote backup operation,
and cleanup serialize on a dedicated helper-stage lock. The finalizer waits for
that lock before acquiring the canonical release and retained-load lock prefix,
then validates the exact run/attempt path, root-owned directory identity, and
any staged bundle/helper manifest before removal. The stage lock remains held
through bundle streaming and by the operation wrapper until its child exits, so
cleanup cannot race a live transfer or helper process after SSH cancellation.
An already-absent stage is a successful cleanup. The finalizer has a 60-second
SSH wall-clock limit and keepalives. If a lock remains busy or any stage
identity/content differs, cleanup fails closed and leaves the unexpected stage
for investigation rather than unlinking it.

The isolated restore helper converts SIGINT and SIGTERM into its rollback and
unit-reap path, including a bounded attempt to stop and reap the systemd
launcher before removing the private mountpoint. SIGKILL or a host/kernel crash
cannot run Python cleanup; the transient unit's runtime cap bounds the scratch
cluster, and any remaining mountpoint requires operator inspection before a
later attempt.

Example, with values copied from the exact security run on the current `dev`
SHA:

```bash
SECURITY_RUN_ID=EXACT_SECURITY_RUN_ID
SECURITY_RUN_ATTEMPT=EXACT_RUN_ATTEMPT
SOURCE_SHA=EXACT_40_HEX_DEV_SHA
gh workflow run platform-production-backup.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f operation=create \
  -f security_run_id="$SECURITY_RUN_ID" \
  -f security_run_attempt="$SECURITY_RUN_ATTEMPT" \
  -f source_sha="$SOURCE_SHA"
```

Dispatch the backup while the exact Security run is still active so its
production job reserves the shared slot before the automatic workflow can
dispatch a deployment child. The auto-deploy workflow waits for that exact
backup run to complete successfully and validates its source-bound receipt;
only then does it make the single child-dispatch request. No deployment child
is queued behind the backup while that proof is pending. Do not dispatch
another production operation in between. The backup invokes the
lock-aware backup-only mode and acquires locks in the fixed order
release -> retained-load -> build -> live-QA. Backup-only does not apply
production-release, source-artifact, transient-browser or live-QA retention.
The workflow verifies that the pre-existing archive inventory is unchanged
and that the new archive is restore/Alembic/checksum/freshness verified. Its
fixed success marker records the exact workflow source SHA, attested bundle
digest, and hashes of the staged storage-maintenance and backup-drill helpers.
Its bounded public receipt contains counts, byte totals and inventory digests;
failure diagnostics stay in a root-private file and are not copied to the
public artifact. No backup-only branch rotates or removes existing archives.
Full storage maintenance owns the separate archive-rotation path and applies
its configured keep count only after a successful restore drill.

The automatic deployment workflow waits for this exact create run before it
dispatches a deployment child. It accepts one completed-success backup run
whose workflow, source Security run ID and attempt, source SHA, `dev` ref,
backup run attempt, production-job start time, artifact API digest and closed
receipt all agree. The backup workflow and production job must have started
while that source Security run was still in progress, proving that the backup
reserved the shared production slot before the automatic child. The receipt
is uploaded only after the remote operation, cleanup and final acceptance
succeed. Missing, expired, duplicate, stale, malformed or mismatched evidence,
an API/ZIP digest mismatch, or a polling deadline causes the auto-deploy job to
fail before its only deployment-dispatch request. The existing age-limited
backup preflight remains an additional check; it does not replace this
source-bound proof. The auto-deploy workflow has no fallback to an older backup
and does not dispatch a manual-deployment path.

When the source-bound scratch restore service exits before its confinement can
be verified, the explicit private-diagnostics path records only a closed unit
phase/state/result/status summary and bounded stream byte counts and hashes in
the private report. Bounded launcher output is retained with the run-bound
private failure capture so the exact startup failure can be diagnosed; it is
never copied into the shared marker or public workflow artifact. Invalid or
oversized diagnostic data fails closed and cannot make a failed restore pass.

After launch, the transient unit must reach `ActiveState=active` with a verified
PID, executable, cgroup and confinement sample within 30 seconds. During that
startup window, only loaded/not-found units reporting inactive, activating or
active are eligible for polling, and only while the launcher remains live.
Inactive or active states must not report a PID or cgroup; an activating state
may have preliminary identity, which is not accepted as confinement. A failed
unit result, launcher exit or unknown/partial identity fails closed. Worker
output is never accepted as restore success unless an active confinement sample
was observed.

### Re-verify the newest existing backup

When the newest archive is present but its restore verification is missing or
failed, use the workflow's `verify-existing` operation. It selects only the
newest sidecar, validates that exact archive and metadata, and runs the same
isolated-cluster restore/Alembic/table checks. It never falls back to an
older pair, creates a dump, rotates pairs, or changes archive bytes. On success
it updates only the selected sidecar's restore-verification fields; the
original completion time remains the age reference. Failure leaves that pair
unchanged. The restore monitor samples free space during each restore and
terminates its owned child at the configured floor plus lead margin; the lead
is not a hard bound on a single write burst.

The workflow offers an explicit `evict_build_node_cache` opt-in for the case
where the validated disposable builder cache is the remaining storage
constraint. It is available only with `verify-existing`. While holding the
release, retained-load, build-output and live-QA locks, the helper validates
the pinned cache manifest and complete tree, checks for process references,
records a durable intent, and removes only the exact pinned build-Node cache.
It then records a durable completion receipt before starting the restore
drill. The cache is regenerable from its pinned archive. A failure after the
intent or during removal stops the workflow for operator review; it does not
start the restore or alter a backup pair. This operation does not evict the
separate live-QA runtime cache.

When an existing-backup restore stops, the root-private run receipt records a
closed restore phase and guard reason, the observed free bytes and required
threshold when available, whether the temporary database was created in the
disposable cluster, and the drop outcome. It records catalog-confirmed absence
only after the exact drill database is checked through the scratch admin
connection. Missing evidence stays unknown; the receipt never includes the
database name, raw command output, exception text, or credentials.

A failed backup creation can also stop at the disk-floor guard before or while
`pg_dump` runs. Its root-private failure receipt records only a fixed phase,
guard reason and available/required byte counts; unavailable measurements stay
null. It never records raw command output or changes the disk floor, restore
lead, or backup-preservation behavior.

For this opt-in only, if the canonical local build-output lock path
`/root/old_sparky/platform/dist/releases` is absent, maintenance initializes
the fixed `dist` and `releases` directories as root-owned mode `0755`
directories. It validates the existing path chain without following symlinks,
requires the same filesystem device, fsyncs each parent after creation, and
then locks the exact `releases` directory inode used by the release builder.
Existing unsafe, replaced, or unexpected paths stop the operation. Ordinary
backup verification does not create these directories and retains its
existing lock behavior.

Dispatch `Platform production backup` from reviewed `dev` with
`operation=verify-existing`, the exact successful security-run ID/attempt and
source SHA, and `evict_build_node_cache=true`. The workflow fetches and
attestation-checks the exact source-bound helper bundle before staging it.
The bounded public result reports only cache status, Node version, allocated
bytes, tree digest and receipt names; it does not publish backup metadata or
private restore diagnostics. Leave the option false when cache eviction is
not required.

Check freshness without restoring production:

```bash
/opt/oldsparky/platform/shared/venv/bin/python \
  /opt/oldsparky/platform/current/tools/platform_backup_restore_drill.py \
  --output-dir /opt/oldsparky/platform/shared/backups \
  --check-latest --max-age-hours 24 --json
```

## Off-host encrypted copy

Off-host backup remains incomplete until all of these are evidenced:

1. separate private `oldsparky-backups` R2 bucket with no `r2.dev` or custom
   domain;
2. separate bucket-scoped read/write token, not the media token;
3. offline-generated OpenPGP recovery key; only the verified public key exists
   on the VPS;
4. root-owned mode `0600` `.env.backup` with the bucket/token and full recovery
   fingerprint;
5. one upload verified by HeadObject and one offline download/decrypt/checksum
   recovery drill.

`platform_backup_offsite.py` validates and encrypts locally by default, deletes
its temporary ciphertext and makes no remote write. `--apply` uploads only the
newest format-v2 restore-verified archive and verifies size/SHA/metadata. It
never deletes remote objects.

```bash
cd /opt/oldsparky/platform/current
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_backup_offsite.py --json
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_backup_offsite.py --apply --json
```

Enable `deadlock-offsite-backup.timer` only after the manual recovery drill.
Do not automate remote deletion during launch hardening. R2 is an off-host copy,
not an immutable vault; retain tested offline ciphertext too.

## Production restore gate

A production restore is destructive and requires explicit operator approval.

1. Stop writes and stop `deadlock-api.service` and `deadlock-worker.service`.
   Record incident/recovery-point ownership; keep both units stopped through the
   restore and cache-purge steps.
2. Identify the exact archive, manifest and Alembic revision; verify checksum
   and decryption.
3. Take a fresh pre-restore snapshot when the database is readable.
4. Restore only to `platformdb`; never use `sparkydb`.
5. Run Alembic head and database-only readiness checks while API and worker
   remain stopped.
6. Purge profile-access cache entries for both cache generations before
   starting either service. The fixed command acquires the normal maintenance
   lock order, verifies both units are inactive, and removes only the three
   source-owned v1 and three v2 profile-access prefixes:

   ```bash
   cd /opt/oldsparky/platform/current
   sudo /opt/oldsparky/platform/shared/venv/bin/python \
     tools/platform_storage_maintenance.py \
     --app-dir /opt/oldsparky/platform \
     --purge-profile-access-cache-after-restore --apply --json
   ```

   A nonzero result or missing `"ok": true` means the purge was not proven;
   keep API and worker stopped and resolve the lock, service-state or Redis
   failure before proceeding. The command does not create a backup, prune
   artifacts, or accept caller-supplied key prefixes.
7. Start API and worker, then run service readiness, role/workflow smoke and
   media reconciliation.
8. Retain the pre-restore evidence and document any data-loss interval.

Routine drills always use a new temporary database in the disposable cluster.
Never downgrade migrations
automatically. Media mapping is in PostgreSQL; after restore use targeted R2
HeadObject checks from DB rows, not a full bucket scan.

Reference: [PostgreSQL backup and restore](https://www.postgresql.org/docs/current/backup.html).
