# Platform backup and restore runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-10-02

## Local verified backup

`platform_backup_supervisor.py` is the sole production DB-backup owner. Its
low-level `platform_backup_restore_drill.py` primitive dumps
`platformdb.platform` plus `public.alembic_version`, writes a private checksum
manifest, restores into a new temporary database, validates tables,
extensions/Alembic and drops the drill database. Daily maintenance retains 14
verified copies.

Production source-release retention is rooted at
`/opt/oldsparky/platform/dist/releases`; apply entrypoints pass this path
explicitly and fail closed if it is unavailable. The restore drill binds the
database to the exact single Alembic head derived from the deployed
`alembic/versions` graph; a missing, branched or mismatched graph is not a
successful verification.

Before any database backup, restore, archive rotation or retention deletion,
the supervisor proves the installed PID-namespace monitor. The maintenance
unit permits only PID namespaces (`RestrictNamespaces=pid`); the off-site unit
does not run database commands and therefore keeps its stricter namespace and
empty capability contract. The monitor path must be a root-owned, regular,
non-symlink executable with safe parent directories.

The operation timeout is one canonical 1500-second budget, passed from the
supervisor CLI through storage to the restore drill. Two seconds are reserved
for bounded cleanup. A restore drill uses a cryptographically random strict
database identifier and first proves that it is absent. A create collision,
non-zero result or timeout does not prove ownership, so the database is never
dropped; machine-readable JSON records `cleanup_unproven` and emits separate
`database_id`/`operator_action` fields only for the exact generated ID and
fixed action `inspect_ownership_before_drop`; invalid IDs are omitted.
Once creation returns success, cleanup uses only the reserved interval. A
cleanup failure is recorded as `cleanup_unproven` and never turns the backup
green.

The manifest is the closed, versioned v2 contract implemented by
[`platform_backup_manifest.py`](../tools/platform_backup_manifest.py). It
requires the ordered `schemas: ["platform", "public"]` value, the exact
`required_extensions: ["pg_trgm"]` list, a unique 32-character `run_id`,
archive size and SHA-256, restore/Alembic status and UTC timing fields. The retired singular
`schema` field is invalid; it is not migrated or interpreted as `schemas`.
The archive and manifest use the same timestamp plus run ID, so two runs in
one second cannot share a dump/manifest pair. Manifest publication is
temporary-file + file fsync + atomic rename + backup-directory fsync. Readers
reject partial JSON, extra or duplicate keys, wrong types/order, symlinks,
hardlinks, unexpected owner/group or mode, path mismatches, checksum and size
drift. A failed restore keeps its allowlisted primary `restore_error` code;
when cleanup is unproven, the manifest separately records
`cleanup_status=unproven` plus the validated generated `database_id` and fixed
`operator_action` when available.

The dump producer reserves both final names with `O_EXCL`, writes `pg_dump`
through a held mode-`0600` descriptor, verifies that descriptor and pathname
identity before publication, and removes the reserved pair on any publication
or directory-fsync failure.

Phase B closes the same-owner writer window with the canonical backup
supervisor and `/run/lock/oldsparky-platform-backup.lock`. The lock is a
root-owned, single-link regular file with mode `0600`; it is opened with a
held descriptor, validated against the pathname, and protected by a
non-blocking kernel `flock` plus a fixed Linux abstract-namespace AF_UNIX
singleton. Replacing the lock filename therefore cannot create a second
owner. A stale filename is safe to reuse, while a symlink, hardlink, pathname
replacement or active owner fails closed. The production API has no
caller-selected lock path; only private tests inject a temporary backend.

All mutating operations use one lock order:
`release -> retained-load -> source/build -> live-QA -> backup`. Local create,
restore-drill and prune use the complete order; off-site select/encrypt/upload
and HeadObject verification use the backup lock as the final suffix. No
operation acquires an earlier lock after the backup lock, and a caller that
already holds predecessors passes an in-process supervisor capability instead
of re-acquiring them.

`platform_backup_supervisor.py` is the sole production mutation owner. It
revalidates the exact dump/manifest pair identity, SHA-256 and size both
before and after consumer work. Off-site selection holds both `O_NOFOLLOW`
descriptors for the complete select/encrypt/HeadObject/PUT transaction; GPG
reads the held dump descriptor and uploads read from a held ciphertext
descriptor, never a reopened pathname. Its private evidence record is written as
`started` before mutation and published atomically after completion. The
closed schema permits `started`, `passed`, `failed`, `blocked`, `cancelled`
and `unknown`; stale/interrupted `.inprogress` records become `unknown`, never
green. A final publication or directory-fsync failure removes the final
receipt and retains an unknown `.inprogress` record; readers reject a final
receipt while any matching in-progress record exists. Evidence contains only bounded release/manifest/checksum, lock,
Alembic, recovery and remote-transport fields—never credentials, raw stderr,
private paths or PIDs.

The offsite timer remains disabled. Enabling it requires a separate reviewed
operator gate after the offline recovery drill. Destructive production restore
is intentionally disabled in the supervisor; routine restore drills use a
new temporary database, and the documented production recovery gate remains
the only future owner.

The read-only health monitor uses this same parser and archive checksum path;
legacy, minimal, extra-key or malformed metadata therefore fails the backup
health check closed.

The low-level create/restore drill is invoked in-process by the supervisor; do
not run its mutating primitive directly on the production host because it does
not acquire the host-wide operation locks. Its mutating create and prune
functions require an unforgeable in-process supervisor capability and refuse
direct calls. Create a production backup through
the lock-aware backup-only mode:

```bash
cd /opt/oldsparky/platform/current
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_backup_supervisor.py maintenance \
  --app-dir /opt/oldsparky/platform \
  --source-release-dir /opt/oldsparky/platform/dist/releases \
  --backup-keep 14 --backup-max-age-hours 24 \
  --backup-timeout-seconds 1500 \
  --backup-only --apply --json
```

For a reviewed `dev` release, an operator may run the same guarded backup
through GitHub Actions without opening a direct production shell:

```bash
gh workflow run platform-production-backup.yml \
  --repo StrayForest/old_sparky \
  --ref dev
```

Wait for the `Platform production backup` workflow to pass before observing
or repeating the automatic production deployment. It invokes the same
lock-aware backup-only mode and acquires locks in the fixed order
release -> retained-load -> build -> live-QA -> backup. Backup-only does not apply
production-release, source-artifact, transient-browser or live-QA retention.
After the supervisor has restore/Alembic/checksum/freshness verified the exact
pair, it may rotate only its own archive/metadata set, bounded to 14 retained
copies. A create or restore failure exits before rotation or any other
pruning path, and leaves all existing backup archives untouched.

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
its temporary ciphertext and makes no remote write. Supervisor `--apply`
uploads only the newest canonical v2 restore-verified archive and verifies
size/SHA/metadata. It consumes the same manifest parser as the creator and
never deletes remote objects.

```bash
cd /opt/oldsparky/platform/current
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_backup_offsite.py --json
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_backup_supervisor.py offsite \
  --app-dir /opt/oldsparky/platform --apply --env-file \
  /opt/oldsparky/platform/shared/.env.backup --platform-env-file \
  /opt/oldsparky/platform/shared/.env.platform --backup-dir \
  /opt/oldsparky/platform/shared/backups --max-age-hours 30 --json
```

The `deadlock-offsite-backup.timer` remains disabled in this phase. Run only
the local dry run while the manual recovery drill and operator enablement gate
remain open. Do not automate remote deletion during launch hardening. R2 is an
off-host copy, not an immutable vault; retain tested offline ciphertext too.

## Production restore gate

A production restore is destructive and requires explicit operator approval.

1. Stop writes and record incident/recovery-point ownership.
2. Identify the exact archive, manifest and Alembic revision; verify checksum
   and decryption.
3. Take a fresh pre-restore snapshot when the database is readable.
4. Restore only to `platformdb`; never use `sparkydb`.
5. Run Alembic head, readiness, role/workflow smoke and media reconciliation.
6. Retain the pre-restore evidence and document any data-loss interval.

Routine drills always use a new temporary database. Never downgrade migrations
automatically. Media mapping is in PostgreSQL; after restore use targeted R2
HeadObject checks from DB rows, not a full bucket scan.

Reference: [PostgreSQL backup and restore](https://www.postgresql.org/docs/current/backup.html).
