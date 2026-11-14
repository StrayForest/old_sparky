# ADR: Release-independent production host-tools generations

- Status: Accepted
- Owner: Platform release maintainers
- Last reviewed: 2026-11-14

## Context

Production deployment requires a small set of privileged host-control tools.
Executing candidate application code with production credentials would let a
reviewed application change its own release authority. The host-control
generation therefore has a separate source identity and provisioning path.

## Decision

Install deployment control as an immutable generation at
`/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>/`. Keep three
identities separate: application source `TARGET_SHA`, host-control source
`HOST_TOOLS_SHA`, and the validated LiveQA runtime provider. The repository pin
[`platform/contracts/host_tools_pin.json`](../../contracts/host_tools_pin.json)
is the only source for `HOST_TOOLS_SHA`; resolving it selects source but does
not prove that its bytes are built, provisioned or installed. [`CURRENT.md`](../CURRENT.md)
owns installed-generation evidence. The deployment runbook owns provisioning,
deployment and recovery procedures.

CI builds and attests the exact pinned bundle from a secret-free job. An
environment-approved gate validates its artifact identity, provenance and
closed manifest before release work. An approved host authority provisions
the generation and passes its isolated installed self-test before production
can use it. A candidate artifact, CI result or successful resolver alone
never establishes installed capability; preserve the previous valid
generation until the new one passes self-test.

Production control executes only the exact immutable dispatcher path with
isolated, bytecode-disabled Python. It does not use a mutable
`current/tools` fallback, self-install from CI, transfer a candidate helper to
the host or execute candidate code in a credential-bearing job. Recovery and
retained-load actions remain limited to fixed host capabilities and exact
source/run bindings defined by the deployment runbook.

## Trusted pull-request candidate handoff

The default-branch
[`platform-host-tools-candidate.yml`](../../../.github/workflows/platform-host-tools-candidate.yml)
workflow is an evidence-only consumer of a successful canonical pull-request
Security run. It runs from the trusted `dev` workflow context with read-only
repository/action/pull-request permissions and narrowly scoped attestation
permissions. It receives no secrets or production write permissions.

The workflow resolves a trusted default-branch commit **T** and executes only
validators and the deterministic bundle builder from **T**. Isolated
`python -I -B` invocations load helpers by their trusted `__file__` path; they
never add candidate code to `sys.path` or import, compile or execute it. The
workflow checks the exact workflow identity, run and attempt, pull request,
source head and canonical repository, then re-reads the bound identities
before attestation and upload. The pull-request checkout **E** is data only.

The trusted pin parser resolves host-tools generation **C** from **E**. If C
is already reachable from the current PR base, the run completes
`eligible=false` without producing a bundle. A novel C reachable from E but
not the base may produce review evidence. This allows a PR to merge-sync
current `dev` before introducing C. Ambiguous or malformed pin history fails
closed. The resulting bounded evidence is explicitly `deployable: false`;
production workflows do not consume candidate artifacts.

### Closed source-versus-tested merge identity

The source head **E** and tested synthetic merge **M** are distinct. The
triggering `workflow_run` snapshot, exact-attempt run API response and current
pull-request response must agree on repository, workflow, run, attempt, base
ref/SHA and source head/ref. The current PR's non-null `merge_commit_sha`
establishes **M** only after the singular pull merge-ref response and commit
response confirm the same merge ref, valid tree and exactly two ordered
parents `[base, E]`. Missing, stale, substituted or raced identities fail
closed; there is no source-head fallback.

The serialized context binds **E**, **M**, merge ref, tree and ordered parents.
The Security summary's `tested_sha` and closed classifier route artifact must
identify **M**, and their manifest digests must agree. Run-job identities and
artifact metadata bind to that exact attempt. Before attestation and upload,
the workflow fetches the run, pull request, merge ref and commit again and
compares the complete immutable context. These checks apply to the
non-deployable pull-request evidence path; production push authority remains
the separate exact-SHA release contract.

## Operator provisioning status

The [host-tools provisioning runbook](../host-tools-provisioning-runbook.md#current-operator-instruction)
records the current hold and conditional installation constraints. It does
not define an executable provisioning procedure: current artifact verification
cannot authorize the missing-generation bootstrap route. Keep the C→P
lifecycle as the accepted design; do not activate P until the runbook's
source-bound bootstrap support is implemented and reviewed.

## Intentional host-tools bump lifecycle

Keep host-control edits and their selecting pin as separate source identities:

1. Commit **C** contains the reviewed host-control changes.
2. Descendant pin-only commit **P** selects C and records the exact closure
   paths, modes and digests. C must be an ancestor of P; the resolver must
   accept the closure at P. Never make a source commit self-pin.
3. Require exact-SHA Security, the source-bound backup and the normal
   automatic production chain. The exact signed artifact and installed
   self-test are separate prerequisites; a pin or bundle is not installation
   proof.
4. If an identity, artifact, provenance, installed capability, release receipt
   or lock-held baseline check differs, stop and retain evidence. Do not
   manually dispatch normal deployment or continue with a failed child.

See the [deployment runbook](../deployment-runbook.md) for exact release
inputs, retry boundaries, failure handling and rollback. The current source
pin is owned by `platform/contracts/host_tools_pin.json`; live installed state
is owned by [`CURRENT.md`](../CURRENT.md).

## Runner and active-application source binding

For an authenticated cumulative no-op, the normal deploy workflow may publish
one source receipt that binds the runner SHA and exact deploy, Security and
AUTO run/attempts to the canonical no-op manifest, artifact identity and full
active-release baseline tuple. This records the already-active application
source; it grants no deployment or provisioning authority.

Load and live-QA workflows keep their checked-out runner SHA as their source
and `target_sha`. They may derive an application target only from the validated
receipt's `baseline_identity.source_sha`, never from caller input. Same-source
runs use the existing schema. A verified no-op mismatch uses one closed
`source_binding` object. The immutable host-tools guard validates the source
relationship, and fixture, cleanup and finalization helpers re-read the full
release tuple under the retained-load lock before production work or release
state changes. Reports retain runner and application identities and the
canonical binding digest.

## Consequences

Separating host control from application releases limits candidate code's
authority in secret-bearing jobs and permits recovery from a broken
application release using independently provisioned tools. The trade-off is
an explicit reviewed provisioning step for every intentional host-tools
change; automatic deployment fails closed until the pinned generation is
installed and self-tested.
