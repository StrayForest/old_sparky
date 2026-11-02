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
- Do not run a production load while free space is below
  `max(5 GiB, 10% of filesystem capacity)` or usage is above 90%; a failed or
  incomplete cleanup is not evidence of a passing run.

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
   Validate the installed maintenance `ExecStart` against the active release,
   then run its exact arguments for the read-only preview:

   ```bash
   current_release="$(readlink -f /opt/oldsparky/platform/current)"
   systemctl show deadlock-maintenance.service \
     --property=FragmentPath,DropInPaths,NeedDaemonReload,ActiveState,SubState,MainPID,ExecStart,CPUQuotaPerSecUSec,MemoryMax,TimeoutStartUSec \
     --no-pager \
     | /usr/bin/python3 -I -B "$current_release/tools/platform_maintenance_qa_evidence.py" \
       run-retention-preview \
       "$current_release/deploy/systemd/deadlock-maintenance.service" \
       /etc/systemd/system/deadlock-maintenance.service
   ```

   Record the `current`/`previous` roles and every explicit
   `--protected-release` value in the validated installed `ExecStart`. The
   canonical retention plan also resolves the shared-venv origin through the
   active release's `platform_verify_venv_reuse.py` and adds it to the
   protected set. A fresh-venv `snapshot` transition resolves to the current
   release; an `unchanged` chain or receipt resolves to its validated older
   origin. Missing or invalid lineage fails the plan before backup or
   deletion. Apply a full sweep only when its fresh preview shows the
   validated origin in `production_releases.retained` and absent from
   `deleted`.

   Run the resolver from the exact active release. It prints only the validated
   origin slug; it fails closed if the active release lacks the CLI option or
   any identity check fails:

   ```bash
   set -euo pipefail
   current_release="$(readlink -f /opt/oldsparky/platform/current)"
   origin_slug="$(/opt/oldsparky/platform/shared/venv/bin/python -I -B \
     "$current_release/tools/platform_verify_venv_reuse.py" \
     --resolve-shared-venv-origin \
     --app /opt/oldsparky/platform \
     --current "$current_release" \
     --venv /opt/oldsparky/platform/shared/venv)"
   test -n "$origin_slug"
   ```

   If an older active-release validator rejects the required `MainPID`
   property, fail closed and retry after a reviewed release installs the
   updated validator; see `platform/docs/operations-runbook.md`. Do not remove
   the property or edit the immutable release.

3. Apply the bounded sweep through the installed service only when the fresh
   exact preview confirms the validated shared-venv origin is retained and not
   deleted. If origin validation fails or the preview lists it in `deleted`,
   do not apply the full sweep; use step 4's release-only procedure or keep
   retention read-only until the origin is protected. When the precondition
   passes, use the reviewed
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

   Keep the workflow ref at `dev`; `expected_sha` is the active deployed app
   SHA. The verifier and both QA artifacts bind to the exact `dev` runner SHA,
   while both reports must name `expected_sha` as their app target. If runner
   and app SHAs differ, each report must carry the authenticated
   `verified-noop` source-binding digest. Never dispatch from an arbitrary SHA
   ref or treat `expected_sha` as the workflow source.

   The workflow verifies the active release and starts
   `deadlock-maintenance.service`; the installed service holds the canonical
   release then retained-load locks while applying the sweep and publishing
   bounded evidence. Diagnostics only sample retained-lock state at one point
   in time; they do not hold that lock during collection.

   Its report is bound to that service invocation. If a valid report confirms
   a restore-verified backup but maintenance exits nonzero because disk
   thresholds still fail, the workflow publishes the bounded result and keeps
   the failure status. Do not infer that no deletion occurred or call cleanup
   successful from an artifact upload alone.

   Read-only diagnostics may upload a bounded partial inventory plus a closed
   stage/outcome when collection fails. Use those fields to distinguish
   transport or producer failure from a disk-threshold result; raw host report
   content and command output remain private.

   The service keeps five newest production releases plus `current`,
   `previous`, the shared-venv origin, and configured pins; it retains one
   live-QA runtime cache and applies age/pattern bounds to other known artifact
   directories. For a paused timer, use the stop, preview, and resume procedure
   in `platform/docs/operations-runbook.md`.

   If a prior exact retained-load cleanup committed `PreprodTestRun` as
   `cleaned` but lost only the final filesystem removal, rerun the exact-ID
   cleanup workflow for that load run. The supervisor verifies the durable
   cleanup identity, confirms the synthetic fixture boundary is empty, and
  removes only that run's root and matching external export directory; mixed or
  incomplete state remains fail-closed.

4. If the full sweep cannot create its backup because the filesystem is
   already full, first use the identity-checked production retention tool
   after an explicit operator decision and a read-only candidate review. Use
   every `--protected-release` value from the validated installed `ExecStart`
   in both preview and apply; never use a bare command that omits those pins:

   ```bash
   set -euo pipefail
   current_release="$(readlink -f /opt/oldsparky/platform/current)"
   origin_slug="$(/opt/oldsparky/platform/shared/venv/bin/python -I -B \
     "$current_release/tools/platform_verify_venv_reuse.py" \
     --resolve-shared-venv-origin --app /opt/oldsparky/platform \
     --current "$current_release" --venv /opt/oldsparky/platform/shared/venv)"
   test -n "$origin_slug"
   # Include the validated origin and every installed ExecStart pin once.
   protected_releases=("$origin_slug" "<exact-installed-pin-1>" "<exact-installed-pin-2>" "<exact-installed-pin-3>")
   # Include the complete candidate list from this exact dry-run.
   expected_candidates=("<exact-preview-candidate-1>" "<exact-preview-candidate-2>" "<exact-preview-candidate-3>")
   retention_args=(--app-dir /opt/oldsparky/platform --keep 5 --min-age-days 0)
   for slug in "${protected_releases[@]}"; do
     retention_args+=(--protected-release "$slug")
   done
   /opt/oldsparky/platform/shared/venv/bin/python \
     "$current_release/tools/platform_release_retention.py" \
     "${retention_args[@]}"
   for slug in "${expected_candidates[@]}"; do
     retention_args+=(--expected-candidate "$slug")
   done
   current_release_now="$(readlink -f /opt/oldsparky/platform/current)"
   origin_slug_now="$(/opt/oldsparky/platform/shared/venv/bin/python -I -B \
     "$current_release_now/tools/platform_verify_venv_reuse.py" \
     --resolve-shared-venv-origin --app /opt/oldsparky/platform \
     --current "$current_release_now" --venv /opt/oldsparky/platform/shared/venv)"
   test "$current_release_now" = "$current_release" && test "$origin_slug_now" = "$origin_slug"
   umask 077
   receipt_file="$(mktemp /run/oldsparky-release-retention.XXXXXX)"
   /opt/oldsparky/platform/shared/venv/bin/python \
     "$current_release/tools/platform_release_retention.py" \
     "${retention_args[@]}" --apply --json-receipt > "$receipt_file"
   /usr/bin/python3 -I -B - "$receipt_file" "${#expected_candidates[@]}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="ascii") as stream:
    receipt = json.load(stream)
expected = int(sys.argv[2])
if (
    set(receipt) != {"schema", "status", "candidate_count", "deleted_count", "reclaimed_bytes"}
    or type(receipt["schema"]) is not int
    or receipt["schema"] != 1
    or receipt["status"] != "passed"
    or type(receipt["candidate_count"]) is not int
    or type(receipt["deleted_count"]) is not int
    or type(receipt["reclaimed_bytes"]) is not int
    or receipt["candidate_count"] != expected
    or receipt["deleted_count"] != expected
    or receipt["reclaimed_bytes"] < 0
):
    raise SystemExit(1)
PY
   ```

   The first invocation is read-only. Use its full candidate set as the exact
   repeated `--expected-candidate` list for apply. Review it against the full
   maintenance preview, accounting for the origin and any other identities
   protected only by this release-only command; do not expect the two sets to
   be equal. Every set difference must be explained by those protected
   identities. If any difference is unexplained, stop and refresh both previews.
   The release-only set must exclude the origin, named pins, and `current`/
   `previous`. Replace sample pin/candidate values with the exact observed
   values; do not guess IDs or counts. Recheck the exact
   candidate identities, release inodes, pointers, locks, and pending
   transactions immediately before apply. The command also re-resolves the
   active release and origin and stops unless both still match the preview.
   If the candidate set or any protected identity changed, stop and produce a
   fresh preview.
   `--expected-candidate` must list the complete remaining deletion set. A
   protected release does not bypass the release lock, pointer checks, or
   candidate revalidation. Keep `protected_releases` unique; the CLI rejects
   duplicate values.

   Signed task-retention wrappers may add `--json-receipt` only with `--apply`
   and the complete `--expected-candidate` set. It emits a fixed success count
   receipt only after the lock-held postcondition confirms every planned path
   is absent; it never prints candidate slugs or paths. Capture stdout in the
   mode-`0600` `/run` file before parsing it. `reclaimed_bytes` is the planner's
   summed file-size estimate, not allocated blocks actually returned to the
   filesystem; do not compare it with `st_blocks` or use it as the disk-space
   postcondition.

   This emergency staging step may delete only the exact reviewed unprotected
   release candidates. Afterward, collect a fresh full-maintenance preview.
   Start `deadlock-maintenance.service` only if that preview still retains the
   validated origin and excludes it from `deleted`; otherwise keep the full
   sweep read-only. Do not use `--skip-backup` for the full sweep.

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
