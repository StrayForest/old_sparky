# ADR: Release-independent production host-tools generations

Status: accepted for the host-capability gate

## Decision

Production deployment control is a release-independent, host-provisioned
generation.  The immutable path is:

`/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>/`

The application release SHA (`TARGET_SHA`) and host-control generation SHA
(`HOST_TOOLS_SHA`) are separate contracts. The repository-owned bounded pin at
[`platform/contracts/host_tools_pin.json`](../../contracts/host_tools_pin.json)
is the only source for `HOST_TOOLS_SHA`. This change follows the A/B pin
lifecycle: source commit A updates the runtime-profile output boundary and its
regression, and pin commit B selects A as generation S11 with its exact
closure. S11 source A is `138128c99025069143f5c234f6cb58545ed8ab1c` and is
installed with provisioning receipt SHA-256
`2f182be1aaf90ee56ed83f7c755e4f38ec860d6f7f20e2ae003084c1a0a7c3f7`. Its
exact-target preflight run `37276328155` passed; independent checkpoint
`69b5ec3bb3c13ed79d07823c227782307fb1c90d65db20adf7ba7590efea9bb8` verified
all 15 members, the isolated self-test and no bytecode. S10
`03e1aed83017779f25df20b43f2ba37e6efacdbc` with receipt
`b0ff2f0483c281538699b1a0104e071b9b319d89b2cf8512c4951f307d394ec7`, S9
`7a9fe1286054dbe96221d2b428048c4212270cf5`, and recovery generation
`d47ae6a278f76bb8a46cca0bbe9019427584c3d2861880e8a7365d87a627be66` remain
preserved. The retained-load export-authority bump uses **C**
`47551d9c9278da79234314aade6aa1ce55d7f979` and pin commit **P**
`b806ff4f141b29d2d970ada36b2e3521cd2ce1e9`; the pin records C's exact
14-member closure. Root provisioning and the exact-P preflight/reconcile remain
pending the reviewed merge and are required before the capability is used. The
follow-up stdin-boundary and cleanup-ownership correction uses **C**
`9b9c0317776b8e788d916b2eb5d4d7ac9cb71af8`; its pin update records the same
14-member closure with the exact changed dispatcher and input-guard digests.
The exact-P preflight and root provisioning remain required before that
generation is used. The sudo-policy denial compatibility correction uses **C**
`f997be4747f70631b79439d02deffd1160bea149` and pin commit **P**
`246184a12e19fcb2b9e3aabf2cd7d767a08d2214`; P records C's exact 14-member
closure. Its retained-load executor accepts only status 0 or 1 paired with the
exact single ASCII denial line for the fixed artifact account and local host.
Granted privilege output, extra output, other statuses and check errors remain
fail-closed. Exact-P provisioning and the installed generation self-test are
required before this generation is used. The
deploy-supervisor inventory alignment uses **C**
`53e99544da7a98e14909f4cd17737cb76e176d22` and pin commit **P**
`626d7c03a79b0a5bff46185bdf4398d547c055a3`; P records C's exact 14-member
closure. C includes `platform_retained_load_export_executor.py` in both the
supervisor's exact manifest inventory and its root-owned helper metadata
checks, matching the dispatcher and signed bundle. Exact-P provisioning and
the installed generation self-test are required before this generation is
used. The
safe-environment interpreter metadata correction uses **C**
`b186fbd177ab82330c1ec001d96a2d40474f9552` and pin commit **P**
`4c954d02494f2f25a9da112bf26e8c02ef6636a2`; P records C's exact 14-member
closure. The fixed production venv Python path is a root-owned, single-link
symlink that must resolve to `/usr/bin/python3.12`. The validator checks the
resolved interpreter as a regular root-owned, single-link file with no
group/world write or set-id bits; it does not treat Linux symlink mode `0777`
as target write permission. The exact manifest-bound payload, source,
approved-tool, script and runtime-ancestry checks remain required. Provisioning
and the installed-generation self-test are required before this generation is
used. The active application release and production health are owned by
[`CURRENT.md`](../CURRENT.md); this ADR does not duplicate those changing facts.
The retained-export pin-comparison correction uses **C**
`97f90674ccc45d6e223ee2793ed3f3c46d928fff` and pin-only **P**
`ef48bd2b6b354a95094dd51260397759b4342445`. P binds the exact 14-member C
closure. The pin records source Git modes (`0644` or `0755`); the builder
normalizes installed tool files to `0555`, with `capabilities.txt` and
`manifest.json` at `0444`. Compare the closed path set and each digest by exact
relative path, and validate source and installed modes against their separate
contracts instead of comparing those mode values directly. The next dispatcher
correction uses **C** `bc1c731454f9f34c1fac8d50ba66960cfe1bf1b7` and pin-only
**P** `a0a3390e9d52c5080c9c4d46fba4643e0201f08b`. P records C's exact
14-member closure; the external cleanup and export-removal branches forward
the validated `load_run_id` and `cleanup_run_id` fields instead of reading an
absent `run_id`. The reviewed source pin is not proof of installation: the
verified production app is `01bde73dffe2c7532be97f6c14c07f32e0f41b90` and the
installed host generation remains
`b186fbd177ab82330c1ec001d96a2d40474f9552` until signed-artifact provisioning
and the installed-generation self-test for C complete.
The live-launch status and bounded-collection correction uses **C**
`f8909e185fd5120606d43a675ce146287be3b10f` and pin-only **P**
`6e877addc8b49880c6b83f7c56d93e6454db4da5`. P records C's exact 14-member
closure. The dispatcher accepts only the fixed SHA-bound live-launch status
record from the installed supervisor; it does not forward child output. The
supervisor treats `oldsparky-platform` as the required live-QA identity while
keeping the legacy `oldsparky` name in collision checks. The bounded collector
limits both retained status bytes and total stream bytes, and verifies process
group closure before reporting completion. Signed provisioning and the
installed-generation self-test for C are required before this generation is
used.
The compact Live QA runtime and shared-venv verifier candidate uses **C**
`032f3879876ab9501b36336219e4d09b95db26b0` and pin-only **P**
`55f52c871055b75847b9b884e168d01c8385b5bc` with the exact canonical 14-member closure. Exact-P full CI and
out-of-band provisioning remain pending; this source pin does not establish
that the generation is installed or active in production.
The pin records the
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
substituted into the trusted generation. The retained-load export executor is
a narrow exception: it is part of the immutable host generation because it
removes only two exact, closed export roots after the workflow has copied and
validated them.

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
Toolset v3 publishes the explicit `python_bytecode_disabled` capability and
the `retained_load_export_cleanup` capability. The
reviewed baseline-capable generation also publishes `release_baseline=1` and
provides the fixed `host-release-baseline` command. That query is root-only and
read-only; it validates the current release receipt with the pinned artifact
validator, rejects an active release/systemd transaction, and emits only the
bounded source/release/pointer identity tuple used by the deployment
supervisor's under-lock recheck. It does not read or execute application code.
For the retained-load capability, the fixed dispatcher accepts marker and
export-removal commands only from its exact immutable host-tools generation,
only when the active release's `TARGET_SHA` matches the closed request and the
release's pin names that same generation and closure. It then validates the
dedicated export account and invokes only the bundled executor through the
fixed `setpriv` command with a closed two-run-ID input. The candidate
application dispatcher cannot create the completion marker or remove these
exports. Fixture setup and database cleanup continue through the current
application dispatcher and retain their exact target/current-release checks.
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
   A v3 archive has exactly 16 top-level members: the 14 declared tools,
   `capabilities.txt` and `manifest.json`. The manifest's `files` array has 15
   records for the 14 tools plus `capabilities.txt`; `manifest.json` is the
   separate sixteenth archive member and is not self-listed. Do not treat the
   record count as the archive member count.
3. Through the approved root-only console or host-image pipeline, extract
   verified bytes into a private staging directory for the exact
   `HOST_TOOLS_SHA`. Before publication, independently check the complete
   top-level name set against those 16 expected members, then verify each
   member is a root-owned regular single-link file with the mode and digest
   bound by the verified bundle and manifest. Reject extra entries such as
   `__pycache__` and reject a missing `manifest.json`, even if all 14 tools
   match. Do not import or execute a staged archive member while validating
   this tree. Create the published generation with a `0555` directory and
   `0555`/`0444` files as specified, and use an atomic no-replace rename only
   after the complete independent check passes. Never install through the
   deployment workflow or execute a downloaded installer or candidate
   repository file on the host.
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
post-copy digest and performs an atomic no-replace rename into the versioned
generation path only after all checks pass. It must not execute an archive
member while staging and must leave the prior generation untouched. The
post-publish self-test is the fixed installed entrypoint, with no candidate
input. Run it with isolated Python and bytecode disabled; do not use an
import-only diagnostic against the published generation:

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
trusted classifier. A pure bootstrap range is a verified no-op. The
authenticated baseline helper may also report operational no-action when the
incoming route is recovery-bootstrap-only and the storage-triggered cumulative
range stays within the exact recovery, storage and docs sets. It preserves the
manifest and baseline; no app build, activation or marker occurs.
A range containing application changes proceeds
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
   As part of this trusted provisioning operation, create the dedicated
   `oldsparky-load-artifacts` system user and same-named primary group only if
   absent. It must have a unique nonzero UID/GID, no supplementary groups, home
   `/nonexistent`, shell `/usr/sbin/nologin`, a locked password, no sudo rule,
   and no collision with API, web, worker or live-QA identities. If an entry
   exists but violates that contract, provisioning stops for operator review;
   it must not change or repurpose the existing account. The app, workflow and
   bundled executor never create or repair this account. For a wholly absent
   account and group, the trusted root operator may create them with:

   ```sh
   /usr/sbin/groupadd --system oldsparky-load-artifacts
   /usr/sbin/useradd --system --gid oldsparky-load-artifacts --no-create-home \
     --home-dir /nonexistent --shell /usr/sbin/nologin oldsparky-load-artifacts
   /usr/sbin/passwd --lock oldsparky-load-artifacts
   ```

   If either name already exists, or a command stops partway through, stop and
   inspect the exact account state instead of retrying or repairing it
   automatically. Verify the resulting contract using the bundled `owner`
   command and retain only its success status in the root provisioning
   receipt. The later `host-capabilities` check must also pass before any
   retained-load workflow uses this capability.
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
