# Host-tools provisioning runbook

- Status: Operator hold — no supported missing-generation bootstrap route
- Owner: Platform release maintainers
- Last reviewed: 2026-11-14

## Current operator instruction

Stop if the exact pinned `HOST_TOOLS_SHA` generation is absent or fails its
host-capability check. Do not install a downloaded bundle, retry the workflow,
dispatch a normal deployment, bypass the capability gate, or treat a source
pin, CI success or attestation alone as installation proof. Preserve the
source SHA, host-tools SHA, exact workflow/run/attempt IDs, artifact identity,
failed job and bounded failure evidence. Escalate to the Platform release
maintainers for a separately reviewed provenance path. The current code has no
approved operator procedure that can finish this missing-generation case.

The automatic workflow builds a root-provisioning bootstrap artifact named
`platform-host-tools-bootstrap-<security-run-id>-<security-attempt>-<auto-attempt>`
in `.github/workflows/platform-production-autodeploy.yml`, then waits for the
installed generation before dispatching the production child. That artifact
is an in-progress `workflow_run` handoff. The generic
`platform_host_tools_bundle.py verify-workflow-attempt` accepts only `push`
and `workflow_dispatch` attempts, so it cannot authorize the AUTO handoff.
The production child also builds
`platform-host-tools-bundle-<run-id>-<attempt>` in
`.github/workflows/platform-production-deploy.yml`; its missing-generation
failure does not expose a trusted parent-to-child dispatch receipt through the
run/attempt API. A successful `mode=preflight` cannot bootstrap a missing
generation because the host-capability check runs before preflight can
succeed. These outputs are not approved operator inputs for this procedure.

Do not substitute the separate LiveQA authorization receipt for production
dispatch evidence. Do not infer mode or parent-child identity from a caller
label, artifact name alone, run time, matching SHA, or a successful unrelated
workflow.

## Support required before provisioning can resume

Platform release maintainers must add and review a source-bound bootstrap path
before operators install a missing generation. It must verify the exact
repository, workflow path, event, branch, in-progress attempt, artifact ID,
name, outer digest, successful builder job, attestation signer and invocation,
and the single bundle subject digest. It must keep separate identities for
the upstream Security `TARGET_SHA`, the workflow execution SHA and pinned
`HOST_TOOLS_SHA`. It must bind the exact Security run/attempt to the AUTO
attempt and persist a trusted parent-to-child dispatch receipt containing the
repository, both run IDs/attempts, mode, Security identity, target SHA and
artifact identity. The bootstrap check must run before the host-capability
gate, so it can safely hand off a missing generation without treating a
successful preflight as its own prerequisite.

Until that support is reviewed and available in the exact source-bound
workflow, the hold above remains in force. This runbook does not define a
manual verifier or allow operator assertions to replace these bindings.

## Conditional installed-generation controls

The following controls apply only after an approved source-bound artifact
verification and provisioning procedure exists. They are required design
constraints, not permission to stage the current AUTO or failed-child artifact.

Select the layout from the verified bundle manifest. Version 3 has 14 tools
plus `capabilities.txt` and `manifest.json` (16 archive members and 15
`files` records); version 4 has 15 tools plus those two files (17 archive
members and 16 `files` records). The manifest's `files` count excludes
`manifest.json`. Reject extra entries, symlinks, missing members and any
metadata or digest mismatch.

An approved root-only authority must stage the exact immutable generation at
`/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>/`. Verify every
member's root ownership, single-link regular-file type, normalized mode and
digest. The generation directory is `root:root`, link count 2 and mode `0555`;
tool files are `root:root`, single-link and `0555`; `capabilities.txt` and
`manifest.json` are `root:root`, single-link and `0444`. Publish only with a
same-filesystem atomic no-replace rename after all checks pass. Keep the
previous valid generation untouched. Never import or execute staged files,
install through a release workflow, change `current`, or repair mismatches by
changing permissions.

After an approved publish, run the fixed installed self-test with isolated
Python and bytecode disabled:

```bash
/usr/bin/python3.12 -I -B \
  /opt/oldsparky/platform/shared/host-tools/$HOST_TOOLS_SHA/platform_workflow_remote_dispatch.py \
  host-capabilities
```

Require exit zero and the bounded `HOST_TOOLS schema=1` contract, including
`release_baseline=1` and `python_bytecode_disabled=1`; confirm there are no
`__pycache__` or `.pyc` entries. Record source and host-tools SHAs, verified
artifact and attestation identities, bundle and manifest digests, pre/post
inventories, self-test result, operator and host-image change ID. Run the
required exact-source read-only preflight before any deployment proceeds.

If a future approved procedure fails during staging, stop. Remove or
quarantine only the exact incomplete staging/new generation through the
approved authority, record post-failure hashes, preserve the previous
generation and retain evidence. Do not rerun only a failed child or continue
around a failed gate; follow the newly approved recovery and cleanup procedure.
