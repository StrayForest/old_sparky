---
name: platform-storage-retention
description: Safely prune OldSparky production release, backup, and post-run QA artifacts with lock, identity, backup, and health checks. Use after every production load or QA run, after a release failure, after storage diagnostics, or whenever stale platform artifacts accumulate.
---

# Platform Storage Retention

Use this skill to close every production load/QA run and to keep the single
origin host below its storage limits. Exact fixture cleanup and filesystem
retention are separate gates: complete both before starting another run or
calling the task complete.

## Invariants

- Operate only on the canonical platform host and `platformdb`; never touch
  `sparkydb`, user data, R2 objects, TLS material, `.env` files, or arbitrary
  `/tmp` paths.
- Keep the `current` and `previous` release targets. Never delete a release
  that becomes either symlink target during the operation.
- Use the deployed retention tools and their release/build/live-QA locks. Do
  not use broad `rm -rf`, ad-hoc age-based deletion, or low-level release
  installers.
- Require explicit operator authorization for a manual production deletion.
  The daily maintenance timer is the standing bounded policy; manual
  invocations still require an operator-approved window.
- Do not run a production load while free space is below 5 GiB or usage is
  above 85%; a failed or incomplete cleanup is not evidence of a passing run.

## Post-run workflow

1. Finish the exact load-owned cleanup first. The external production-load
   workflow owns its fixture cleanup. If a run was canceled or stopped before
   its final report, use the matching retained-load abort/recovery workflow
   or `platform-production-retained-load-cleanup.yml` with the exact run ID
   and control account. Never replace exact cleanup with a marker-wide or
   historical database deletion.
2. Collect a bounded storage inventory before filesystem deletion:

   ```bash
   gh workflow run platform-production-storage-diagnostics.yml \
     --repo StrayForest/old_sparky --ref dev \
     -f expected_sha=<exact-source-sha-currently-deployed>
   ```

   On the production host, confirm `current`, `previous`, absence of
   `.release-operation.json`, active service state, and the candidate list.
   Use the deployed tool in dry-run mode:

   ```bash
   /opt/oldsparky/platform/shared/venv/bin/python \
     /opt/oldsparky/platform/current/tools/platform_storage_maintenance.py \
     --json
   ```

3. Apply the bounded sweep through the installed service using the reviewed
   production workflow. It creates a fresh restore-verified backup before
   deleting release candidates, source build artifacts, bounded browser
   artifacts, screenshots, and stale live-QA runtime caches:

   ```bash
   gh workflow run platform-production-storage-maintenance.yml \
     --repo StrayForest/old_sparky --ref dev \
     -f confirmation=APPLY-PRODUCTION-STORAGE-MAINTENANCE \
     -f expected_sha=<exact-source-sha-currently-deployed>
   gh run watch <maintenance-run-id> --repo StrayForest/old_sparky --exit-status
   ```

   The workflow holds the retained-load barrier, verifies the active release,
   starts `deadlock-maintenance.service`, and publishes bounded evidence.

   The service keeps five newest production releases plus `current` and
   `previous`; it retains one live-QA runtime cache and applies age/pattern
   bounds to other known artifact directories.

   If a prior exact retained-load cleanup committed `PreprodTestRun` as
   `cleaned` but lost only the final filesystem removal, rerun the exact-ID
   cleanup workflow for that load run. The supervisor verifies the durable
   cleanup identity, confirms the synthetic fixture boundary is empty, and
   removes only that run's root; mixed or incomplete state remains fail-closed.

4. If the full sweep cannot create its backup because the filesystem is
   already full, first use the identity-checked production retention tool
   after an explicit operator decision and a read-only candidate review:

   ```bash
   /opt/oldsparky/platform/shared/venv/bin/python \
     /opt/oldsparky/platform/current/tools/platform_release_retention.py \
     --app-dir /opt/oldsparky/platform --keep 5 --min-age-days 0 --apply
   ```

   This emergency staging step may delete only unprotected old release
   directories. Run `deadlock-maintenance.service` immediately afterward so
   the fresh restore-verified backup and remaining retention checks complete.
   Do not use `--skip-backup` for the full sweep.

5. Verify and record the result before another run:

   ```bash
   df -hT /
   df -ih /
   readlink -f /opt/oldsparky/platform/current
   readlink -f /opt/oldsparky/platform/previous
   for service in deadlock-api deadlock-worker deadlock-web; do
     systemctl is-active --quiet "$service" || exit 1
   done
   curl -fsS --max-time 10 http://127.0.0.1:8010/api/v1/health/live >/dev/null
   curl -fsS --max-time 10 http://127.0.0.1:8010/api/v1/health/ready >/dev/null
   curl -fsS --max-time 10 http://127.0.0.1:3000/ >/dev/null
   ```

   Confirm the maintenance result is `ok=true`, the backup is
   `restore_verified=true`, no pending release transaction remains, and
   `current`/`previous` are unchanged. Preserve the run ID, maintenance
   report, backup metadata, and final disk/health evidence in the handoff.

## Failure boundaries

- A backup or restore-drill failure stops deletion in the full sweep. Inspect
  the maintenance journal and resolve the specific failure; do not repeatedly
  retry a production load or silently bypass the backup gate.
- A held release/build/live-QA lock means wait or inspect the owner. Do not
  kill the owner process or delete its temporary files.
- An unexpected symlink, ownership, inode, pending transaction, service state,
  or candidate-list change is fail-closed. Re-run the read-only inventory.
- Storage retention never replaces exact production fixture cleanup, and exact
  fixture cleanup never authorizes broad storage deletion.

## Source of truth

Use `platform/docs/operations-runbook.md` for the operator procedure,
`platform_storage_maintenance.py` for the bounded combined sweep,
`platform_release_retention.py` for the emergency release-only staging step,
and `platform-production-storage-diagnostics.yml` for read-only remote
evidence. Keep production cleanup in the reviewed operator channel; do not
invent a new SSH path or expose secrets in reports.
