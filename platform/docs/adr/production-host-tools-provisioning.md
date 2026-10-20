# ADR: Release-independent production host-tools generations

Status: accepted for the host-capability gate

## Decision

Production deployment control is a release-independent, host-provisioned
generation.  The immutable path is:

`/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>/`

The application release SHA (`TARGET_SHA`) and host-control generation SHA (`HOST_TOOLS_SHA`) are separate contracts. The repository-owned bounded pin at
[`platform/contracts/host_tools_pin.json`](../../contracts/host_tools_pin.json)
is the only source for `HOST_TOOLS_SHA`. Historical pin P
`43783b31699d498776a70b2a644b081d828cd41b` selected
`fcbfe3dc41c999ae298a8b1f958a07f9c1fd0ff1` for source A's exact 15-member closure. Preparation snapshot, pending installation: source C3 `8ea7678183ac1bd600d06a628cbf78c49d029672` and pin-only P3 `5cd50fc992272f20ec8a7fa773df9af9a8346fb2`; the exact-P3 resolver passes, and `safe_env` is the sole changed `HOST_TOOL_FILES` member in its pinned closure. This pair supersedes C2 `995f46f9b522b3446375346ce71d8c7cc57eb280` and P2 `e562024a9b55c5aec60d333ee8e31542ac779adf`, which introduced typed browser-link preservation and a bounded two-engine QA preflight. Exact-P3 CI, signed provenance, approved provisioning and the installed-generation self-test remain required; selection and resolver success do not prove installation. The active installed generation remains recorded in [`CURRENT.md`](../CURRENT.md), separate from `HOST_TOOLS_SHA` and application `TARGET_SHA`.
S11 source A is `138128c99025069143f5c234f6cb58545ed8ab1c` and is
installed with provisioning receipt SHA-256
`2f182be1aaf90ee56ed83f7c755e4f38ec860d6f7f20e2ae003084c1a0a7c3f7`. Its
exact-target preflight run `37276328155` passed; independent checkpoint
`69b5ec3bb3c13ed79d07823c227782307fb1c90d65db20adf7ba7590efea9bb8` verified
all 15 members, the isolated self-test and no bytecode. S10
`03e1aed83017779f25df20b43f2ba37e6efacdbc` with receipt
`b0ff2f0483c281538699b1a0104e071b9b319d89b2cf8512c4951f307d394ec7`, S9
`7a9fe1286054dbe96221d2b428048c4212270cf5`, and recovery generation
`d47ae6a278f76bb8a46cca0bbe9019427584c3d2861880e8a7365d87a627be66` remain
preserved. SSR diagnostics use C `d2cb3b2ba0425e08d60904f442240400b199db12`
and pin-only P `7c40b6fd5e035238c6d43ea206357eca8e6aa53a`; P records the exact
15-member closure. Exact-P CI, preflight, signed-bundle verification, root
provisioning and self-test remain required; the pin does not prove installation.
The retained-load export-authority bump uses **C**
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
The complete-archive, space-bounded bootstrap extraction follow-up uses **C2**
`382e2b93a69e0352a70feea474f33559f34d28b0` and pin-only **P3**
`13e635a7d8cb1c5d5c00bbcffca34af1f547e28f`, with the exact ordered
14-member closure. Exact-P full CI, signed provisioning and the installed
self-test remain required; these source commits do not establish production
installation or activation.
Source generation **T** `532fd2ecefd53d19e7099f0cb9076c19fb0644dc` adds the
separate, disabled-by-default SSR heap metrics selector to web runtime
rendering and the static-8 diagnostic profile. It emits only bounded numeric
RSS/V8 heap/GC samples; it does not change request behavior, admission,
resource budgets or production service state. The prior source generation
adds a closed pre-supervisor `dispatch` result to the existing source-bound
live-launch status parser; it does not loosen the supervisor's failure-stage
allowlist. Source generation **R**
`3bde55d29ab7e7b1102b0f1f36608f7ffd4a73ab` makes the production supervisor
select the heap-metrics key whenever it applies a managed runtime profile.
The static-8 profile therefore enables the reviewed sampler, while baseline
and other profiles explicitly write their reviewed false value. Baseline
rollback selects the same key and clears the diagnostic setting. Pin-only
commit **S** selects R and records its exact ordered 14-member closure; it
does not change the installed host generation. The earlier immutable generation
`f5365c6a56a7e8dad21054e8ae69e0001914adae` remains a separate historical pin;
that historical pin does not establish its continued presence or which
generation is active. Native installed-generation attestation and the
production retention ledger are the authorities for that state. The
bounded `host-capabilities` self-test for the new generation must still include
`release_baseline=1`, `retained_load_export_cleanup=1`,
`retained_load_source_binding=1`, and `python_bytecode_disabled=1` before the
active app may accept a runner/app source mismatch. This pin is source
provenance, not installation evidence: the new generation still requires
ordinary exact-S full-CI/preflight attestation and approved out-of-band root
provisioning and self-test at
`/opt/oldsparky/platform/shared/host-tools/3bde55d29ab7e7b1102b0f1f36608f7ffd4a73ab`.
Do not infer that the new generation or final pin-bearing application SHA is
installed from this repository value.
The bounded LiveQA count protocol and authenticated-read-admission candidate
use source **C** `bbd7fd6f46dfbd21c3dfd6b1b27f6409c580196e`; its pin-only child
selects C and updates exactly the dispatcher and shared-environment config
digests in the 14-member closure. The selected 24x8 profile keeps the existing
32-slot admitted-plus-waiting envelope and temporarily enables only the
bounded numeric heap sampler. Exact-C CI, signed provisioning and installed
self-test are still required; this source pin does not prove activation. The fixed-enum early Live User QA failure-boundary diagnostics use source **C** `a45d5e9cd42b7a738cea76ee8e3dc411d79b36c3` and pin-only **P** `e8557e430958019941f6482975cdbde4223e28a8`; only `platform_workflow_remote_dispatch.py` changes within the 15-member host-tools closure, and exact-P full CI, signed preflight, out-of-band provisioning and installed-generation self-test remain required. The release-diagnostics and test-impact preflight update uses **C** `d33abd32194074d8331ea19bc9c77523afbdf688` and pin-only **P** `25a0a65d9cd20fd0da3f95b5f3920314cdd9dc48`; its 15-member closure changes only the dispatcher and supervisor, adding closed venv-policy stages and stable recovery workflow-identity validation. Exact-P full CI, signed preflight, provisioning and installed self-test remain required; neither commit proves installation.
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
shell preflight consumers. Its three components are kept explicit:

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

The trusted candidate validator supports two exact source layouts while this
transition is in progress: the installed `production-host-tools-v3` closure
with 14 tools and three components, and a `production-host-tools-v4` candidate
closure with those same 14 tools plus `platform_cpu_diagnostic_plan.py` in a
fourth `cpu_diagnostic_control` component. The candidate parser accepts only
those fixed ordered paths, component groups and capability tuples. It parses
the candidate declarations as bounded literals and never imports or executes
candidate code. Adding v4 support to the trusted validator does not mean that
v4 is installed: provisioning evidence must still prove the exact installed
generation before any v4 capability is considered available.

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

1. Record the successful exact-`TARGET_SHA` Security run. A successful `build-host-tools` job
   and attested bundle qualify from (a) read-only `mode=preflight` at that same merged `dev`
   SHA; (b) the exact automatic parent in its active 1,800-second pre-child readiness window,
   only if `TARGET_SHA` is current `dev`, its source-bound restore-verified backup and bundle
   build/attestation/upload succeeded, and no child, app write, pending release transaction or
   competing production operation exists; or (c) the exact automatic child whose only failure
   was the missing pinned generation, with prior checks passed and no build, pending state or
   production write. For (b), provision before the deadline; the parent verifies the generation
   before dispatching its child. No other failure qualifies. Bind `TARGET_SHA`, `HOST_TOOLS_SHA`,
   builder job and full Sigstore signature/chain/subject/signer/source/invocation to exact runs.
   The parent artifact ID/name/digest must bind its exact parent run/attempt, independent of the
   source Security attempt; use its emitted name. Download through the approved channel and
   verify the outer SHA-256 before opening. A pull-request candidate artifact is never a
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

The performance follow-up candidate extends the previous 14-member host
closure with the fixed CPU diagnostic plan controller, producing a
15-member, four-component bundle. The new `cpu_diagnostic_control` component
contains only `platform_cpu_diagnostic_plan.py`; the installed immutable
dispatcher may invoke it only as its exact trusted sibling through the closed
`cpu-diagnostic-plan` stdin action. The capability contract adds
`cpu_diagnostic_plan_control=1` and advances the dispatcher schema to 4. This
candidate is not authorized by an earlier pin: the normal C/P sequence must
record all 15 source paths, modes and digests, pass exact-SHA CI, and provision
and attest the new generation before the action can be used. The 14-member
counts below are historical records for their respective prior generations.

Changes to any member of the host-control closure, its closure declaration or
the bundle helper must be handled as a reviewed **C → P** source sequence,
never by pinning the commit that carries the pin itself:

The 2026-10-09 performance candidate uses source commit
`93f8a8e0f7a2cb32d2444e2d69ae5c09ba669dee` as **C**. Its host-tools closure
delta is limited to `platform_configure_shared_env.py` and
`platform_validate_release_artifact.py`; the application and browser-runtime
changes remain outside that 14-member closure. The pin-bearing **P** records
the raw SHA-256 digests and Git modes of all 14 files from **C**. This source
identity alone does not establish installation or deployment.

The follow-on CPU diagnostic controller uses source **C** `80af72d5fec4c868897baf3cd0172af1c17b3fa6` and pin-only **E** `4af43c5afd84af29bd40d91e9e3002ab540142c5`; E selects the exact 15-member, four-component v4 closure and records its source modes and digests. This does not establish installation: verified T at `7e5769ba3f8596cb9950735f3685835013eb1085` still used v3; exact-SHA CI, approved provisioning and a post-provision capability check remain required.

The source-quality correction **C2** `7c4a4b43434b3d104c060778583f44d0bb22e682`, descended from **E**, changes only the dispatcher and CPU-plan controller bytes; the 15-member v4 layout and capabilities are unchanged. Pin-only **E2** `801e785840adce76528389e47cd22cfa9c6b62d5` names C2 and records its exact closure digests. Neither commit proves C2 provisioned or installed.

The capability-order compatibility correction uses **C3** `16eee656fbca10c485728b34b8490917b4de64a2`, descended from **E2**; it changes only the v4 capability tuple order accepted by the trusted builder, leaving the 15-member closure, capability set and component groups unchanged. Pin-only **E3** `dfb291ca0ca31e001aecc58f2c75e4e06eddf26b` names C3 and records its closure digests; resolver selection does not prove provisioning or installation.

Source **C4** `6d9f593e2214dacfda55d119b2090e7264ef1ab7` adds external-load cleanup diagnostics/finalization; its only 15-member host-tools closure change is `platform_workflow_remote_dispatch.py`. Pin-only **P4** `2082b89780e7990bace7f5b00a60382b89971a8f` selects C4 with the exact closure digest. Exact-P CI, read-only preflight/signed bundle, approved provisioning and installed self-test remain required; neither SHA proves installation.

The bounded SSR follow-up uses source **C5** `d2afef17db081bf435514fa59b6e1d1b3e41c838`: it bounds server-side read-only workspace/auth requests, adds sampled admission-shed aggregates and their public projection, and restores the SSR workspace-error flag with `authenticated-read-admission-24x8`. Its only host-tools change is the supervisor restore selector; the other 14 pinned helpers, modes, v4 layout and capability contract are unchanged. Pin-only **P5** `a2e8dfaa16962074c59047428d22ed2f823cd521` selects C5 and records that supervisor digest. CI, signed artifact verification, approved provisioning and deployment remain separate required evidence.

The populated-quiesce recovery follow-up uses source **C6** `af425b0500d72fd7dbd9a1a48fd35c5d37a59b48` and pin-only **P6**
`112653973b37e38e06293c8c4cde8ab2854062a4`. Its 15-entry v4 closure changes
only `platform_workflow_remote_dispatch.py` and
`platform_production_deploy_supervisor.sh` from C5. C6 adds separately authorized signed preservation
of populated quiesce candidates and pruning only a fully reviewed task-created
release set; the empty-only abort remains unchanged. Exact CI, signed bundle,
approved recovery/provisioning and post-action checks remain required; neither
SHA proves installation.

The backup START/DONE barrier and source-bound receipt use **C7** `56036d18604df69da2f8ac79ce7196142a803a0f` and pin-only **P7**
`c5f80abdc41ac012ec97cf305c01340cfa4aec09`. P7 records C7's 15-member closure.
Full CI, signed preflight, approved provisioning and installed self-test remain required; neither SHA proves C7 is installed or active in production.

The CPU-diagnostic first-fault and exact abort-evidence follow-up uses source **C8** `f0f4b460ea56075febab9b53ce42c21d776d6675` and pin-only **P8** `cf90411596ae4cca7a810727d74740d4437b64d5`. Its 15-member v4 closure changes only `platform_cpu_diagnostic_plan.py` and `platform_workflow_remote_dispatch.py`; P8 records their exact modes and digests. Exact-P8 CI, signed preflight, approved provisioning and installed self-test remain required. Neither commit proves that C8 is provisioned or active in production.

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

## Runner and active-application source binding

A successful cumulative baseline-reconcile may be a verified no-op when the
new runner commit changes only an explicitly approved, non-deployable
host-tools or recovery-evidence path family. Such a run must not create an
application release just to make the runner SHA equal the already-active
application SHA. Instead, the normal deploy workflow publishes one
authenticated no-op source receipt. It binds the runner SHA and exact deploy,
security and auto-deploy run/attempts to the canonical cumulative no-op
manifest, artifact ID/name/digest, and the complete active-release baseline
tuple. The receipt is evidence of the already-active source; it grants no
provisioning or deployment authority.

Load and live-QA workflows keep the checked-out runner SHA as their source and
`target_sha`. They derive a separate application target only from the
validated receipt’s `baseline_identity.source_sha`; there is no caller-supplied
target override. Same-source runs continue to use the existing schema without
a receipt. For a verified no-op mismatch, C2 receives the runner SHA plus one
closed nested `source_binding` object. The immutable C2 guard validates that
object and the source relationship, and the active fixture/cleanup helper
re-reads the complete nine-field release tuple while holding the retained-load
lock. It refuses before publishing readiness or performing database/browser
work if the tuple or installed capability differs. Finalization and export
cleanup repeat the tuple check before touching release state. Reports retain
both runner and application identities and the canonical binding digest, so
candidate, origin, evaluator and sanitized evidence cannot conflate them.

Delegated binding preserves the immutable host-tools pin: the provider is
verified against its manifest/provenance, and installed C2/P must include the
app-side capability before accepting runner/application mismatch.

## Consequences

Source **C** `5849001b78aed824527f474c4c3586108019c402` updates the 15-member v4 pin with the dispatcher’s bounded Python-helper launch and export-removal status protocol. Application workflow and CPU-pair changes remain app-side. This is provenance, not installation proof; production requires exact-target CI, signed preflight/provenance, approved provisioning and installed self-test.

The host-control trust boundary is independent of application releases and cannot be repaired by candidate code in a secret-bearing job. A host image or operator must provision each reviewed generation before deployment; this is the rollback/supply-chain trade-off for preventing candidate source from becoming production authority.

Source **C** `c635240ba9e146e12e1d3a4d43c65224bf87553e` adds bounded private public-browser failure reports and makes validated failed counts fail the outer launch even when Playwright exits zero. Pin-only **P** `e78b290344d288ae9b137390871b3d28187ade09` records its exact 15-member closure, including dispatcher SHA-256 `5d62f944185af3926705b563844ff819268cf7827ca9d7cb5b01398a8da12da6`. Exact-P CI, signed preflight, approved provisioning and installed self-test remain required. At PR preparation on 2026-10-20, installed **C** was `5849001b78aed824527f474c4c3586108019c402` per CURRENT; this is historical state, not activation proof.
