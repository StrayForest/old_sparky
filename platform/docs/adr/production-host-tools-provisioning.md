# ADR: Release-independent production host-tools generations

Status: accepted for the host-capability gate

## Decision

Production deployment control is a release-independent, host-provisioned
generation.  The immutable path is:

`/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>/`

The application release SHA (`TARGET_SHA`) and host-control generation SHA
(`HOST_TOOLS_SHA`) are separate contracts. The repository-owned bounded pin at
[`platform/contracts/host_tools_pin.json`](../../contracts/host_tools_pin.json)
is the only source for `HOST_TOOLS_SHA`; it currently pins the reviewed
generation `a5139603106263bbc68606a03efd44418fb04c48`. This repository pin is
not evidence that the generation is installed: production remains on its
previous generation until a root-console provisioning receipt verifies the
new path. The pin records the
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
approved host-image/configuration-management authority must perform the
following once for a reviewed `HOST_TOOLS_SHA`; the GitHub workflow must not be
used as the installer:

1. Record the successful exact-`TARGET_SHA` `Platform security and build`
   run/attempt. From the separately authorized read-only `mode=preflight`
   workflow run at that same merged `dev` SHA, record the exact successful
   `build-host-tools` job/attempt, artifact ID/name, outer artifact digest,
   application `TARGET_SHA` and pinned `HOST_TOOLS_SHA`. Verify that the
   preflight run performed no release install or production write. Download
   its ZIP through the approved artifact channel and verify its SHA-256 before
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
full `Platform security and build` workflow. Because a host-control-only
change is non-deployable, it does not receive an automatic deployment run.
After full CI succeeds for exact **P**, the documented read-only
`workflow_dispatch` `mode=preflight` at **P** runs `build-host-tools`: it
resolves **C** from **P**, checks out **C**, builds and signs the exact bundle,
and publishes the artifact before the capability check. That downstream
read-only capability check may fail closed because **C** is not yet
provisioned. The successful exact `build-host-tools` job and its artifact
attestation from that preflight run, together with exact-P full-CI evidence,
are the reviewed provisioning inputs. Do not use the separate pull-request
candidate artifact as the authoritative provisioning input. The source
identities remain separate: **P** is the preflight run's application target
and `HOST_TOOLS_SHA=C` names the bytes installed at the generation path. Never
require `TARGET_SHA == HOST_TOOLS_SHA`; verify the exact producer run,
artifact identity, ancestry and closure instead.

After the signed **C** bundle is installed and its receipt is recorded, the
bootstrap-only range is activated through the automatic
`mode=baseline-reconcile` path. That path validates the exact successful
full-CI source, authenticates the current host release against its successful
deployment proof, reclassifies the complete first-parent range and repeats the
baseline check under both production locks before any write. A bootstrap-only
range ends as a verified no-op; a mixed range proceeds only if the complete
range satisfies the ordinary full deployable route, including runtime gates
when required. An absent/mismatched generation or unproven baseline fails
closed. The ordinary `mode=deploy` route is not manually dispatched to
activate this bootstrap change.

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
