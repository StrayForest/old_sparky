# Platform backup and restore runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-08-08

## Local verified backup

`platform_backup_restore_drill.py` is the only DB-backup owner. It dumps
`platformdb.platform` plus `public.alembic_version`, writes a private checksum
manifest, restores into a new temporary database, validates tables,
extensions/Alembic and drops the drill database. Daily maintenance retains 14
verified copies.

Create a release backup:

```bash
cd /opt/oldsparky/platform/current
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_backup_restore_drill.py \
  --env-file /opt/oldsparky/platform/shared/.env.platform \
  --output-dir /opt/oldsparky/platform/shared/backups \
  --keep 14
```

For a reviewed `dev` release, an operator may run the same guarded backup
through GitHub Actions without opening a direct production shell:

```bash
gh workflow run platform-production-backup.yml \
  --repo StrayForest/old_sparky \
  --ref dev
```

Wait for the `Platform production backup` workflow to pass before observing
or repeating the automatic production deployment. It serializes with the
production deploy concurrency group and does not bypass the release preflight.

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
