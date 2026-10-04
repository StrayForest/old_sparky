# ADR: Release-independent production host-tools generations

Status: accepted for the host-capability gate

## Decision

Production deployment control is a release-independent, host-provisioned
generation.  The immutable path is:

`/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>/`

The application release SHA (`TARGET_SHA`) and host-control generation SHA
(`HOST_TOOLS_SHA`) are separate contracts. The repository-owned bounded pin at
[`platform/contracts/host_tools_pin.json`](../../contracts/host_tools_pin.json)
is the only source for `HOST_TOOLS_SHA`; pending pin target
`b39cc48b6d0ad4e5d399d8c286f0b2cf510adf21` selects generation
`0af4a88f130a550a86accab13f2930ba9366c118` (C6). The pin resolves to that
source commit, which is its ancestor, and changes only the supervisor digest
in the 13-member closure. C6 is not provisioned yet. C5 remains the installed
generation, built and attested from merged target
`468b08fef78462bfa605c6c9d43cd5d5e2ca3e49`; its root-owned provisioning
receipt is
`/var/tmp/oldsparky-host-tools-provisioning/0d9d80b7a7d4365abaaf7875e88442b5f43ed46f/provisioning-receipt.json`.
The application release is a separate identity and remains at the release
documented in [`CURRENT.md`](../CURRENT.md) until an exact successful
automatic deployment and smoke are recorded there. The pin records the
expected repository, exact lowercase commit and a closure baseline of paths,
source modes and digests. The resolver requires that commit to be a reachable
ancestor of the reviewed application target. There is no `current` or
application-SHA fallback.

The generation directory is `root:root`, regular, link-count 2 and mode
`0555`.  Every member is a root-owned regular file with link-count 1 and mode
`0555` (the manifest and capability data are still non-executable `0444`
content contracts).  The bundle manifest binds the source SHA, toolset version,
component closure, capabilities, POSIX-relative filenames, numeric Unix modes
and SHA-256 digests.  The generated `files.modes` sidecar serializes those
modes as the conventional octal text emitted by `stat -c %a` (`444`/`555`) for
shell preflight consumers.  Its two components are kept explicit:

- `prepare_artifact`: the fixed dispatcher, input guard and artifact-directory
  helper;
- `production_deploy_control`: the supervisor, release lock/preflight,
  standalone artifact validator, safe-environment/render helpers,
  edge-policy/update helpers and shared-environment/storage evidence
  configuration.

Application runtime files, current-release run wrappers and the candidate
release deploy closure are not host-tools members. Release installation,
transaction recovery, wheelhouse validation, deploy smoke and backup/restore
drill helpers remain candidate/runtime or operator-owned tools and are never
substituted into the trusted generation.

## Workflow boundary

`Platform production deploy` resolves the pin from the exact target checkout,
then checks out the pinned `HOST_TOOLS_SHA` and runs that pinned bundle helper
on a secret-free runner. The bundle manifest/source/generation are therefore
bound to `HOST_TOOLS_SHA`; the artifact API envelope remains bound to the
workflow's application `TARGET_SHA`. The environment-approved capability job
downloads the exact artifact ID and digest, verifies the closed manifest
offline, and checks the already-installed `HOST_TOOLS_SHA` generation. It does
not upload, install or execute any bundle member and it does not check out
candidate source. The remote checks
use only fixed absolute `/usr/bin/id`, `/usr/bin/stat`, `/usr/bin/sha256sum`,
`/usr/bin/test`, `/usr/bin/find` and `/usr/bin/base64` operations against the
exact generation path. The SSH
identity invariant is explicit: the configured deployment identity must
return `id -u == 0`; a non-root identity is a closed failure, not an implicit
sudo fallback. GitHub's artifact metadata is bound by artifact ID, name,
source SHA and digest; because its nested `workflow_run` object may omit
`run_attempt`, the secret-free `build-host-tools` job validates the exact attempt through the
authoritative `/actions/runs/<run_id>/attempts/<run_attempt>` response with
bounded typed JSON checks (the response's optional `ref` is not used because
GitHub may return it as `null` for `workflow_dispatch`). The artifact response
is fetched as bounded raw JSON and validated by the reviewed canonical verifier
(including exact
`digest`, `size_in_bytes`, and downloaded archive size); it is never reduced
to a delimiter-encoded shell string.

The release build is downstream of this gate.  The production consumer invokes
only the exact immutable dispatcher path with `/usr/bin/python3.12 -I -B`.
Toolset v2 publishes the explicit `python_bytecode_disabled` capability. The
reviewed baseline-capable generation also publishes `release_baseline=1` and
provides the fixed `host-release-baseline` command. That query is root-only and
read-only; it validates the current release receipt with the pinned artifact
validator, rejects an active release/systemd transaction, and emits only the
bounded source/release/pointer identity tuple used by the deployment
supervisor's under-lock recheck. It does not read or execute application code.
The dispatcher also rejects a host-generation invocation that omits `-B`
before loading its sibling guard, so a failed caller cannot leave a truncated
`__pycache__` member in the generation:

`/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>/platform_workflow_remote_dispatch.py`

There is no mutable `current/tools` fallback, self-installer, SCP of host
tools, `bash -s`, heredoc remote program or candidate-code execution in the
secret-bearing job.  External-load and retained-cleanup workflows remain
fail-closed on their existing trusted boundaries until they receive an
equivalent reviewed generation capability; this ADR does not silently broaden
those workflows.

## Trusted pull-request candidate handoff

The default-branch [`platform-host-tools-candidate.yml`](../../../.github/workflows/platform-host-tools-candidate.yml)
workflow is a non-deployable `workflow_run` consumer of a successful canonical
`Platform security and build` pull-request run.  Its job runs only from the
trusted `dev` workflow context, has read-only repository/action/pull-request
permissions plus the narrowly scoped provenance-attestation permissions, and
receives no secrets, status/deployment write permission or production
environment approval.

The workflow resolves an immutable trusted default-branch commit **T** and
executes only validators and the deterministic bundle builder from **T**. Its
isolated `python -I -B` invocations load the candidate validator's sibling
bundle helper by the validator's trusted `__file__` path; they never add the
candidate checkout to `sys.path` or import candidate code. It checks the exact
workflow ID/name/path, run ID/attempt/conclusion/head and canonical repository,
then re-reads the same run attempt (including its canonical `pull_requests`
payload) and pull request before attestation and again before artifact upload.
The pull request checkout **E** is data only: it is never imported, compiled,
interpreted or executed. The trusted pin parser resolves host-tools generation
**C** from **E**. A strict-ancestor **C** that is already reachable from the
current PR base is a valid existing generation and completes as `eligible=false`
without building, attesting or uploading an artifact. A novel
`C ∈ reachable(E) \ reachable(base)` is `eligible=true` and may produce the
review artifact. This deliberately accepts a PR branch that merge-syncs
current `dev` before introducing **C**; the merge-sync makes the base reachable
from **E**, while **C** remains reachable only from the PR head. Malformed,
unrelated or otherwise ambiguous pin history fails closed. The exact fixed
closure and its digests remain required.

The inner deterministic bundle is attested before upload.  Candidate and
evidence artifact names bind the pull request, **C**, **E**, security run and
attempt; each uploaded ZIP is read back through bounded metadata/digest checks
and a closed one-member archive check.  The resulting evidence JSON is
mode-0600, size-bounded and explicitly `deployable: false`.  Production
workflows do not consume either candidate prefix, so this handoff can only
produce review evidence; it cannot dispatch, provision, install or deploy.

### Closed source-versus-tested merge identity

The handoff keeps the PR source head **E** separate from the tested synthetic
merge **M**.  The triggering `workflow_run` snapshot records the exact
canonical workflow, run ID, run attempt, PR number, base ref/SHA and source
head/ref; for a `pull_request` run, `workflow_run.head_sha` is the source head
**E**.  The exact-attempt run API response and its embedded PR snapshot must
agree with that event and with the current PR response.  The current PR's
non-null `merge_commit_sha` establishes **M** only after the singular
`GET /git/matching-refs/pull/<N>/merge` response and `GET /commits/<M>` response
confirm the same merge ref, a valid tree and exactly two ordered parents
`[base, E]`.  Missing, null, stale, substituted or raced values are closed
failures; no source-head fallback is permitted.

The serialized context includes the source/base repositories and refs, **E**,
**M**, merge ref, tree and ordered parents.  The security summary's
`tested_sha` must equal **M**.  If a producer adds split source/base/tree/parent
provenance, the consumer accepts only one documented closed shape and compares
every field exactly.  The classifier route artifact is an additional
defense-in-depth check: its target must be **M**, its manifest must be closed,
and its digest must equal the summary's manifest digest.  The exact-attempt
jobs response and artifact metadata must bind every supplied row identity
(`run_id`, `run_attempt`, source head **E**, source head ref and workflow name)
to the same run; commit-status metadata is advisory and is not treated as
producer authority.

Before either attestation or upload, the workflow fetches the exact run and
latest run snapshot, PR, merge ref and commit again and compares the complete
immutable context.  This catches synchronize, base/head/merge-ref/tree and
rerun races.  These checks apply to the candidate `pull_request`/
`deployable=false` path only; production push/deploy authority remains the
separate classifier and release contract.

## One-time operator provisioning (out of band)

This repository change prepares and verifies the handoff only. An operator or
approved host-image/configuration-management authority performs provisioning
for a reviewed `HOST_TOOLS_SHA`; the GitHub workflow must not be used as the
installer:

1. Record the successful exact-`TARGET_SHA` `Platform security and build`
   run/attempt. Use a successful `build-host-tools` job and its attested bundle
   from either a read-only `mode=preflight` run at that same merged `dev` SHA,
   or the exact normal automatic production run for that SHA when the
   host-capability gate failed solely because this pinned generation was
   missing. For the automatic run, also verify the successful automatic
   dispatch, its exact production child attempt, all preceding provenance and
   host-contract checks, and that the child did not begin the release build,
   create pending release state or perform a production write. Any other
   failure is not provisioning evidence. Bind the successful builder job and
   artifact ID/name/outer digest, application `TARGET_SHA`, pinned
   `HOST_TOOLS_SHA`, and attestation to their exact run/attempts. Download the
   ZIP through the approved artifact channel and verify its SHA-256 before
   opening it. The pull-request candidate artifact is not an acceptable
   substitute.
2. Run the offline `platform_host_tools_bundle.py verify` command from the
   pinned reviewed checkout with the expected `HOST_TOOLS_SHA`. Retain the
   generated manifest/capability checksum files with the provisioning record.
3. Through the approved root-only console or host-image pipeline, install the
   validated members atomically at the exact versioned generation path.  Create
   the directory with `0555`, files with `0555`/`0444` as specified, and verify
   root ownership, link counts, type and every recorded digest after the copy.
   Never install through the deployment workflow and never execute a downloaded
   installer or candidate repository file on the host.
4. Keep the previous valid generation untouched.  Before allowing deployment,
   run the same inventory checks recorded by the workflow.  If any check fails,
   remove only the incomplete new generation through the approved authority
   and continue using the previous generation; do not repoint `current` and do
   not weaken the gate.

The evidence record must include the exact application `TARGET_SHA`, pinned
`HOST_TOOLS_SHA`, GitHub artifact ID, outer artifact digest, inner bundle
digest, verifier output, pre/post inventory and the operator/host-image change
ID. The reviewed offline check is
deterministic and can be run before the privileged provisioning action:

```bash
HOST_TOOLS_SHA=<40-lowercase-hex-host-tools-sha>
BUNDLE=/secure/handoff/platform-host-tools-bundle.zip
CONTRACT=/secure/handoff/platform-host-tools-contract
/usr/bin/sha256sum "$BUNDLE"
/usr/bin/python3 -I platform/tools/platform_host_tools_bundle.py verify \
  --bundle "$BUNDLE" --expected-source-sha "$HOST_TOOLS_SHA" --contract-dir "$CONTRACT"
```

The approved root-console/configuration-management operation then extracts
only the verifier-accepted regular members into a private staging directory
named for `HOST_TOOLS_SHA`, applies the manifest modes/ownership, verifies every
post-copy digest and performs an atomic `rename` into the versioned generation
path only after all checks pass.  It must not execute an archive member while
staging and must leave the prior generation untouched.  The harmless post-copy
self-test is the fixed installed entrypoint, with no candidate input:

```bash
/usr/bin/python3.12 -I -B \
  /opt/oldsparky/platform/shared/host-tools/$HOST_TOOLS_SHA/platform_workflow_remote_dispatch.py \
host-capabilities
```

The self-test must print only the bounded `HOST_TOOLS schema=1 ...` contract,
including `release_baseline=1` and `python_bytecode_disabled=1`, and return
zero. The baseline query is separately exercised only after the capability
gate confirms this exact generation. It must not create
`__pycache__` or `.pyc` entries.  A non-zero result, any metadata/digest mismatch or an
interrupted staging action is a failed provisioning attempt: quarantine/remove
only that identified incomplete staging/generation through the approved
authority, retain the previous valid generation, record post-failure hashes and
do not retry by changing permissions or using `current/tools`.

The code commit **C** and its pin-bearing target **P** are separate identities.
The reviewed change places the host-control edits in **C**, followed by a
pin-only-with-respect-to-host-control commit **P** that names **C** and records
the exact closure digests. **C** must be an ancestor of **P**; the resolver
must accept the exact pin and closure at **P**. The pull request and its
synthetic merge are tested together, and the exact merged **P** must pass the
full `Platform security and build` workflow. A host-control-only cumulative
range is non-deployable by itself. The active automatic chain may route that
exact recovery-bootstrap-only range through `mode=baseline-reconcile`; do not
dispatch this mode manually. The production workflow authenticates the exact
source-CI and automatic caller attempts, reads the installed host baseline
through the pinned generation, verifies successful deployment proof for the
baseline source using its unique latest bot-authored deploy marker, canonical
attempt URL and successful exact `Deploy production` job. One baseline-only
compatibility adapter accepts the historical root-validated slug
`gha-<run_id>-1-<UTC build timestamp>` with a bare run URL only when both carry
the same run ID and the current GitHub run API still reports attempt 1. The
exact attempt-1 run, source, bot status, workflow and successful job checks
remain mandatory; unknown slugs, reruns, incomplete pages or ambiguous proof
fail closed. New status markers use exact attempt URLs. It then checks
first-parent ancestry to **P** and reclassifies the complete range with the
trusted classifier. A pure bootstrap range is a verified no-op. A range
containing application changes proceeds
only when the cumulative route is full, deployable and non-fallback, with exact-target
runtime gates when required. Immediately before protected release work, the
pinned supervisor rereads and compares the complete baseline tuple while
holding both production locks. **P** remains the application target and
`HOST_TOOLS_SHA=C` names the bytes at the immutable generation path; never
require `TARGET_SHA == HOST_TOOLS_SHA` or treat the CI artifact as deployment
authority.

## Intentional host-tools bump lifecycle

Changes to any member of the host-control closure, its closure declaration or
the bundle helper must be handled as a reviewed **C → P** source sequence,
never by pinning the commit that carries the pin itself:

1. Commit **C** changes the host-control closure. The previously installed pin
   cannot authorize the modified closure, so ordinary deployment remains
   blocked.
2. Commit **P**, descended from **C** in the same reviewed change, updates only
   the pin with respect to host control: `host_tools_sha` becomes **C** and the
   exact closure baseline records **C**'s paths, modes and digests. The
   application target may contain tests or documentation alongside this pin
   update; the host-control closure itself must match **C** byte for byte.
3. Require full CI success for exact merged **P**, then use the documented
   read-only `mode=preflight` run at **P** to produce the signed
   `build-host-tools` bundle. Verify its exact workflow run/attempt, successful
   builder job, artifact ID/name/digest, inner manifest and closure. The
   downstream capability check may stop the preflight run because **C** is
   not yet installed. The signed bundle is evidence for provisioning, not
   deployment authority; the pull-request candidate artifact is not the
   provisioning source.
4. Provision and self-test `/opt/oldsparky/platform/shared/host-tools/<C>/`
   through the approved root-only console or host-image authority. Record **P**
   as `TARGET_SHA` and **C** as `HOST_TOOLS_SHA`; preserve both identities and
   the artifact provenance in the receipt.
5. Activate the bootstrap-only range through the authenticated automatic
   baseline-reconcile path. Its exact full-CI and runtime proofs, current
   deployed-source proof, cumulative classifier result and lock-held host
   recheck remain mandatory.

If that automatic child stopped only because **C** was absent at the
read-only host-capability gate, do not rerun that child or only its failed
jobs. After provisioning and self-testing **C**, run a successful read-only
`mode=preflight` at the unchanged **P**. If **P** is still the current `dev`
HEAD, its exact successful source-CI proof remains valid, the deployed host
tuple is unchanged and no operation is pending, rerun **all jobs** on the exact
completed `Platform production auto-deploy` workflow run that authorized the
child. This preserves the original source/ref and triggering actor while
advancing the auto-deploy attempt; its dispatch job must create a fresh
production child bound to that new auto run ID/attempt. Verify the new auto
and child run IDs/attempts and their exact success markers before accepting
the release. If any precondition changed, stop and follow the normal exact-SHA
automatic chain for the current `dev` HEAD. Never manually dispatch
`mode=deploy` or rerun only the failed production child as a continuation.

This sequence avoids an impossible self-referential merge-SHA pin while
ensuring the reviewed generation is built, attested, provisioned and tested
before it gains deployment authority. Do not point the pin at a mutable branch,
copy a generation, upload an installer, self-install from CI, bypass the
baseline proof or manually dispatch normal deployment to repair a missing
generation. Repeat **C → P**, signed provisioning and automatic reconcile for
the next intentional bump.

## Consequences

The host-control trust boundary is independent of application releases and
cannot be repaired by a candidate commit during a secret-bearing job.  A host
image or operator process must therefore provision each reviewed generation
before that SHA can deploy.  This intentional operational cost is the
rollback/supply-chain trade-off for preventing candidate source from becoming
the production authority.
