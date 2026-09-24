# ADR: Release-independent production host-tools generations

Status: accepted for the host-capability gate

## Decision

Production deployment control is a release-independent, host-provisioned
generation.  The immutable path is:

`/opt/oldsparky/platform/shared/host-tools/<40-lowercase-hex-source-sha>/`

The generation directory is `root:root`, regular, link-count 2 and mode
`0555`.  Every member is a root-owned regular file with link-count 1 and mode
`0555` (the manifest and capability data are still non-executable `0444`
content contracts).  The bundle manifest binds the source SHA, toolset version,
component closure, capabilities, POSIX-relative filenames, modes and SHA-256
digests.  Its two components are kept explicit:

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

`Platform production deploy` builds the deterministic ZIP and attests it on a
secret-free runner.  The environment-approved capability job downloads the
exact artifact ID and digest, verifies the closed manifest offline, and checks
the already-installed generation.  It does not upload, install or execute any
bundle member and it does not check out candidate source.  The remote checks
use only fixed absolute `/usr/bin/id`, `/usr/bin/stat`, `/usr/bin/sha256sum`,
`/usr/bin/test`, `/usr/bin/find` and `/usr/bin/base64` operations against the
exact generation path. The SSH
identity invariant is explicit: the configured deployment identity must
return `id -u == 0`; a non-root identity is a closed failure, not an implicit
sudo fallback. GitHub's artifact metadata is bound by artifact ID, name,
source SHA and digest; because its nested `workflow_run` object may omit
`run_attempt`, the capability job validates the exact attempt through the
authoritative `/actions/runs/<run_id>/attempts/<run_attempt>` response with
bounded typed JSON checks.

The release build is downstream of this gate.  The production consumer invokes
only the exact immutable dispatcher path with `/usr/bin/python3.12 -I`:

`/opt/oldsparky/platform/shared/host-tools/<target-sha>/platform_workflow_remote_dispatch.py`

There is no mutable `current/tools` fallback, self-installer, SCP of host
tools, `bash -s`, heredoc remote program or candidate-code execution in the
secret-bearing job.  External-load and retained-cleanup workflows remain
fail-closed on their existing trusted boundaries until they receive an
equivalent reviewed generation capability; this ADR does not silently broaden
those workflows.

## One-time operator provisioning (out of band)

This repository change prepares and verifies the handoff only.  An operator or
approved host-image/configuration-management authority must perform the
following once for a reviewed source SHA; the GitHub workflow must not be used
as the installer:

1. Record the exact GitHub artifact ID, outer artifact digest and source SHA
   from the successful `build-host-tools` job.  Download the ZIP through the
   approved artifact channel and verify its SHA-256 before opening it.
2. Run the offline `platform_host_tools_bundle.py verify` command from a
   reviewed trusted checkout with the expected source SHA.  Retain the
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

The evidence record must include the exact merge SHA, GitHub artifact ID,
outer artifact digest, inner bundle digest, verifier output, pre/post inventory
and the operator/host-image change ID.  The reviewed offline check is
deterministic and can be run before the privileged provisioning action:

```bash
MERGE_SHA=<40-lowercase-hex-merge-sha>
BUNDLE=/secure/handoff/platform-host-tools-bundle.zip
CONTRACT=/secure/handoff/platform-host-tools-contract
/usr/bin/sha256sum "$BUNDLE"
/usr/bin/python3 -I platform/tools/platform_host_tools_bundle.py verify \
  --bundle "$BUNDLE" --expected-source-sha "$MERGE_SHA" --contract-dir "$CONTRACT"
```

The approved root-console/configuration-management operation then extracts
only the verifier-accepted regular members into a private staging directory
named for `MERGE_SHA`, applies the manifest modes/ownership, verifies every
post-copy digest and performs an atomic `rename` into the versioned generation
path only after all checks pass.  It must not execute an archive member while
staging and must leave the prior generation untouched.  The harmless post-copy
self-test is the fixed installed entrypoint, with no candidate input:

```bash
/usr/bin/python3.12 -I \
  /opt/oldsparky/platform/shared/host-tools/$MERGE_SHA/platform_workflow_remote_dispatch.py \
  host-capabilities
```

The self-test must print only the bounded `HOST_TOOLS schema=1 ...` contract
and return zero.  A non-zero result, any metadata/digest mismatch or an
interrupted staging action is a failed provisioning attempt: quarantine/remove
only that identified incomplete staging/generation through the approved
authority, retain the previous valid generation, record post-failure hashes and
do not retry by changing permissions or using `current/tools`.

The first deployment after provisioning is still an ordinary reviewed `dev`
push/automatic chain.  A missing or mismatched generation blocks before release
build, attestation, artifact SCP, pending status or production writes.

## Consequences

The host-control trust boundary is independent of application releases and
cannot be repaired by a candidate commit during a secret-bearing job.  A host
image or operator process must therefore provision each reviewed generation
before that SHA can deploy.  This intentional operational cost is the
rollback/supply-chain trade-off for preventing candidate source from becoming
the production authority.
