# ADR: Release-independent production host-tools generations

Status: accepted for the host-capability gate

## Decision

Production deployment control is a release-independent, host-provisioned
generation.  The immutable path is:

`/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>/`

The application release SHA (`TARGET_SHA`) and host-control generation SHA
(`HOST_TOOLS_SHA`) are separate contracts. The repository-owned bounded pin at
[`platform/contracts/host_tools_pin.json`](../../contracts/host_tools_pin.json)
is the only source for `HOST_TOOLS_SHA`; it currently pins the installed
generation `4233e3ce3395da6948192f14e50af2033774f4f0`. The pin records the
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
Toolset v2 publishes the explicit `python_bytecode_disabled` capability.  The
dispatcher also rejects a host-generation invocation that omits `-B` before
loading its sibling guard, so a failed caller cannot leave a truncated
`__pycache__` member in the generation:

`/opt/oldsparky/platform/shared/host-tools/<HOST_TOOLS_SHA>/platform_workflow_remote_dispatch.py`

There is no mutable `current/tools` fallback, self-installer, SCP of host
tools, `bash -s`, heredoc remote program or candidate-code execution in the
secret-bearing job.  External-load and retained-cleanup workflows remain
fail-closed on their existing trusted boundaries until they receive an
equivalent reviewed generation capability; this ADR does not silently broaden
those workflows.

## One-time operator provisioning (out of band)

This repository change prepares and verifies the handoff only. An operator or
approved host-image/configuration-management authority must perform the
following once for a reviewed `HOST_TOOLS_SHA`; the GitHub workflow must not be
used as the installer:

1. Record the exact GitHub artifact ID, outer artifact digest, application
   `TARGET_SHA` and pinned `HOST_TOOLS_SHA` from the successful
   `build-host-tools` job. Download the ZIP through the
   approved artifact channel and verify its SHA-256 before opening it.
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
including `python_bytecode_disabled=1`, and return zero.  It must not create
`__pycache__` or `.pyc` entries.  A non-zero result, any metadata/digest mismatch or an
interrupted staging action is a failed provisioning attempt: quarantine/remove
only that identified incomplete staging/generation through the approved
authority, retain the previous valid generation, record post-failure hashes and
do not retry by changing permissions or using `current/tools`.

The first deployment after provisioning is still an ordinary reviewed `dev`
push/automatic chain.  A missing or mismatched generation blocks before release
build, attestation, artifact SCP, pending status or production writes.

## Intentional host-tools bump lifecycle

Changes to any member of the host-control closure, its closure declaration or
the bundle helper must be handled as a two-commit bump, never by pinning the
merge commit that carries the pin itself:

1. Commit **A** changes the host-control closure. Its old pin is expected to
   fail the closure-baseline gate; this is the useful proof that a closure
   change cannot silently ship under the installed generation.
2. Provision and self-test the exact bundle built from A out of band at
   `/opt/oldsparky/platform/shared/host-tools/<A>/` before merging the pin
   update. The operator records the full lowercase A SHA, repository and
   post-copy inventory.
3. Commit **B** is pin-only with respect to host control: it sets
   `host_tools_sha` to A and replaces the exact closure baseline with A's
   paths, modes and digests. B must be a descendant of A, and the resolver
   must accept B while rejecting any later target that changes the closure
   without another bump. Application-only commits after B continue to reuse A.

This order avoids an impossible self-referential merge-SHA pin while requiring
the reviewed exact generation to exist before the pin-bearing change lands. Do
not point the pin at a mutable branch, copy a generation, upload an installer,
self-install from CI or manually rerun a release to repair a missing
generation. Repeat the A/provision/B lifecycle for the next intentional bump.

## Consequences

The host-control trust boundary is independent of application releases and
cannot be repaired by a candidate commit during a secret-bearing job.  A host
image or operator process must therefore provision each reviewed generation
before that SHA can deploy.  This intentional operational cost is the
rollback/supply-chain trade-off for preventing candidate source from becoming
the production authority.
