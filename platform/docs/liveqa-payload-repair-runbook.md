# LiveQA payload integrity repair

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-11-23

Use this exceptional procedure only after source-bound verification proves that
the active immutable LiveQA payload differs from its manifest by exactly these
two root-owned CPython 3.12 files:

- `platform/tools/__pycache__/platform_live_qa_guard.cpython-312.pyc`
- `platform/tools/__pycache__/platform_live_qa_machine_lock.cpython-312.pyc`

The manifest, all other files and links, active provider closure, current and
previous app releases must validate. The active-source mismatch must be limited
to this pair. Any other missing, changed, extra or unsafe entry is a different
failure: stop and use the applicable recovery owner. Never delete files
manually, ignore bytecode caches in the manifest, or call the runtime installer
directly.

## Prepare and apply

The dedicated `platform-liveqa-payload-repair.yml` workflow is separate from
`platform-production-autodeploy.yml`, so its concurrency queue cannot wait
behind AUTO's backup proof. Each operation authenticates the exact successful
`Platform security and build` push run/attempt and requires its SHA to remain
the current `dev` head. The checked-out source is compared with that SHA before
bundle creation. The host accepts only the fixed four-entry source bundle
(repair helper, LiveQA guard, machine-lock owner, and closed digest manifest),
then runs the helper from stdin with `/usr/bin/python3.12 -I -B` under the
canonical release lock. The helper validates that inherited lock, acquires the
canonical LiveQA lock second, and loads guard/lock code only from the verified
root-private runner bundle. It does not import the damaged LiveQA payload.

1. Record the active `current` source SHA, the successful Security run ID and
   attempt, and the matching AUTO run. `AUTO` allows 9,000 seconds for its
   source-bound backup proof; prepare and apply must finish before that proof
   deadline.
2. Dispatch prepare from `dev`:

   ```bash
   gh workflow run platform-liveqa-payload-repair.yml \
     --ref dev \
     -f operation=prepare \
     -f expected_active_source_sha=<active-current-sha> \
     -f security_run_id=<security-run-id> \
     -f security_run_attempt=<security-run-attempt>
   ```

   Wait for the exact dispatch run to finish. It rechecks the active and
   previous release identities, manifest/tree, exact cache pair, pending
   operations, collected transient QA unit, cgroup/process idleness and both
   locks. Preserve its single `plan_sha256` output. The private descriptor is
   mode `0600`, bound to the observed pointer/manifest/cache identities and is
   not an Actions artifact.
3. Confirm the prepare result is successful for the same Security attempt and
   active source and reports `extra_file_count=2`. Apply only that reviewed
   plan SHA with the same exact inputs:

   ```bash
   gh workflow run platform-liveqa-payload-repair.yml \
     --ref dev \
     -f operation=apply \
     -f expected_active_source_sha=<active-current-sha> \
     -f security_run_id=<security-run-id> \
     -f security_run_attempt=<security-run-attempt> \
     -f plan_sha256=<prepare-plan-sha256>
   ```

   Apply repeats every binding under both locks and atomically renames the
   whole cache directory with no-replace semantics. It then runs the strict
   active-payload verifier. Only `status=validated` means the cache is
   quarantined and the manifest validates. On a timeout or lost SSH response,
   rerun apply only with the same plan SHA; its closed receipt resumes a
   prepared/quarantined transition or confirms an already validated result.
   If an interrupted rollback restored the cache, apply verifies it, marks the
   old plan spent and refuses reuse; prepare a new plan only after that exact
   restoration is confirmed. A changed plan, malformed receipt or unexpected
   filesystem state fails closed.
4. Leave the root-private quarantine, plan and receipts in place. Immediately
   create or verify the single restore-verified backup bound to the same
   Security run/attempt/SHA using the [backup procedure](backup-restore-runbook.md#local-verified-backup).
   AUTO then continues through its ordinary host-readiness and deploy chain.
   If its proof deadline expired or `dev` moved, stop and use the documented
   backup recovery procedure. Do not create a duplicate backup, push an empty
   commit, or manually dispatch normal deployment.
5. After normal deployment and canonical Public Live QA finish, retain the
   quarantine until the storage-retention owner approves its later cleanup.
   This repair workflow does not prune its own evidence.

## Failure handling

The helper accepts only the two fixed cache paths and refuses any other tree
delta, symlink, ownership/mode change, pointer/manifest/provider drift, lock
contention, pending transaction, active QA process/cgroup or uncollected
transient unit. Before strict validation succeeds, it restores only the exact
moved directory on failure. After validation succeeds, it keeps the quarantine
even if the final receipt write fails. A receipt is closed to four fields and
binds the plan SHA, phase and private quarantine-directory identity. Unknown
states require read-only inspection through the same source-bound owner path;
never remove a quarantine, partial source bundle or cache by pathname alone.

`-I` ignores `PYTHON*` environment variables and `-B` prevents new bytecode
writes. `-B` does not prevent Python from reading an existing `.pyc`, so the
repair loads its guard and machine-lock source from the verified runner bundle
and inspects active payload files as data until the bytecode directory has been
quarantined.
