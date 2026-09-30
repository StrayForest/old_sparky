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
generation recorded in that contract. The pin records the expected repository,
exact lowercase commit and a closure baseline of paths,
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
workflow's application `TARGET_SHA`. The separate secret-free
`verify-host-tools` job downloads the raw GitHub artifact API ZIP, validates its
API digest/size and exact one-member outer envelope, extracts the returned
inner bytes with the pinned helper, verifies the closed inner bundle and
regenerates contract sidecars from those verified bytes. It then verifies the
external GitHub build attestation against the exact inner digest, workflow,
issuer, ref and run/attempt. Only scalar results cross the environment
approval boundary. The environment-approved capability job checks the
already-installed `HOST_TOOLS_SHA` generation; it does not download, install or
execute any bundle member and it does not check out candidate source. The
remote checks
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

The inner deterministic bundle is attested before upload. Candidate and
evidence artifact names bind the pull request, **C**, **E**, security run and
attempt; each uploaded ZIP is read back through bounded metadata/digest checks
and a closed one-member archive check. The evidence verifier compares the
returned evidence member byte-for-byte with the local JSON produced by the
trusted validator, and rejects mutation, duplicate members or a mismatched
artifact/API pairing. The resulting evidence JSON is mode-0600, size-bounded
and explicitly `deployable: false`. Production workflows do not consume either
candidate prefix, so this handoff can only produce review evidence; it cannot
dispatch, provision, install or deploy.

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

This repository owns the bounded envelope validator and root-side installer,
but the installer is deliberately operator-side tooling and is not part of the
installed 13-script closure. An approved host-image/configuration-management
authority must perform the operation below for a reviewed `HOST_TOOLS_SHA` **C**;
the GitHub workflow must not install it. The artifact must already have passed a
separate approved, pinned attestation verifier. `platform_host_tools_bundle.py`
does not claim cryptographic attestation verification: it requires that external
receipt as an input gate and only checks its exact tuple.

Before the root command, record the artifact ID/name, application/source-head
SHA **E**, trusted default-branch source **T**, tested synthetic merge **M**,
packaging commit, pinned generation **C**, outer artifact SHA-256, inner bundle
SHA-256, manifest SHA-256, capabilities SHA-256 and the external attestation
receipt. The outer ZIP is bounded and must contain exactly one regular member
named `platform-host-tools-bundle.zip`; the inner ZIP is bounded and must
contain exactly the 15 flat `platform-host-tools/*` members—no 16th member.
Traversal, backslashes, duplicate/ZIP64/special/link members, compression
bombs and digest or identity changes fail closed.

The receipt is closed schema v2. It must contain exactly the allowlisted
verifier ID, issuer `https://token.actions.githubusercontent.com`, repository,
workflow name/path, `refs/heads/dev`, `workflow_dispatch`, production run and
attempt, artifact ID/name, outer and inner subject digests, security run and
attempt, and the exact **C/E/T/M** plus packaging tuple. The mandatory
`--expected-receipt-sha256` is the SHA-256 of the raw receipt obtained through
an independent trusted channel; it must not be copied from a field in the
receipt or invented by this installer. The installer binds that raw receipt to
the command arguments and does not claim to perform cryptographic attestation.

Run this exact command from the reviewed checkout that contains the canonical
helper (mutation: creates one new versioned generation; it does not touch
release pointers, the database or systemd):

```bash
HOST_TOOLS_SHA='<40-lowercase-hex-C>'
SOURCE_HEAD_SHA='<40-lowercase-hex-E>'
PACKAGING_COMMIT='<40-lowercase-hex-packaging-commit>'
ARTIFACT_ID='<positive-decimal-GitHub-artifact-id>'
ARTIFACT_NAME='platform-host-tools-bundle-<run-id>-<run-attempt>'
TRUSTED_SOURCE_SHA='<40-lowercase-hex-T>' TESTED_MERGE_SHA='<40-lowercase-hex-M>'
SECURITY_RUN_ID='<positive-decimal-security-run-id>' SECURITY_RUN_ATTEMPT='<positive-decimal-security-run-attempt>'
OUTER_SHA256='<64-lowercase-hex-outer-digest>' INNER_SHA256='<64-lowercase-hex-inner-digest>' MANIFEST_SHA256='<64-lowercase-hex-manifest-digest>' CAPABILITIES_SHA256='<64-lowercase-hex-capabilities-digest>'
EXPECTED_RECEIPT_SHA256='<64-lowercase-hex-raw-receipt-digest>'
OUTER_BUNDLE=/secure/handoff/platform-host-tools-artifact.zip
ATTESTATION=/secure/handoff/attestation-gate.json
INSTALL_EVIDENCE=/secure/handoff/host-tools-install-evidence.json
HOST_TOOLS_ROOT=/opt/oldsparky/platform/shared/host-tools
/usr/bin/python3.12 -I -B platform/tools/platform_host_tools_bundle.py install \
  --outer-bundle "$OUTER_BUNDLE" \
  --host-tools-root "$HOST_TOOLS_ROOT" \
  --expected-source-sha "$HOST_TOOLS_SHA" \
  --expected-outer-sha256 "$OUTER_SHA256" \
  --expected-inner-sha256 "$INNER_SHA256" \
  --expected-manifest-sha256 "$MANIFEST_SHA256" \
  --expected-capabilities-sha256 "$CAPABILITIES_SHA256" \
  --artifact-id "$ARTIFACT_ID" \
  --artifact-name "$ARTIFACT_NAME" \
  --attestation-evidence "$ATTESTATION" \
  --source-head-sha "$SOURCE_HEAD_SHA" \
  --packaging-commit "$PACKAGING_COMMIT" \
  --trusted-source-sha "$TRUSTED_SOURCE_SHA" \
  --tested-merge-sha "$TESTED_MERGE_SHA" \
  --security-run-id "$SECURITY_RUN_ID" \
  --security-run-attempt "$SECURITY_RUN_ATTEMPT" \
  --expected-receipt-sha256 "$EXPECTED_RECEIPT_SHA256" \
  --evidence-output "$INSTALL_EVIDENCE"
```

The helper requires UID and EUID 0, a no-symlink root-owned parent chain with
no untrusted write access (a root-owned sticky system temporary directory is
allowed), and a same-filesystem private stage under `host-tools`, with
`O_EXCL|O_NOFOLLOW` full writes and `fsync` of every file/stage/parent. It applies `root:root`, `0555`
to all 13 scripts and `0444` to `manifest.json`/`capabilities.txt`, checks the
exact inventory and digests, then publishes with Linux `renameat2`+
`RENAME_NOREPLACE`; `os.replace`, plain `mv` and overwrite are not accepted.
Existing generations and `current`/`previous` are never modified. The helper
runs both fixed self-tests (`host-capabilities` and `host-contract`) with
`/usr/bin/python3.12 -I -B`, and rejects any `__pycache__`/`.pyc` or extra.

The bounded mode-0600 evidence JSON contains the artifact ID, exact C/E/T/M/
packaging tuple, all outer/inner/manifest/capability digests, exact inventory and both self-test
results. A failed or interrupted operation cleans or quarantines only the
identity-checked stage/generation it created; an identity mismatch is left for
the operator rather than recursively deleting anything. Stop immediately on
any failure and investigate before release build, attestation status,
artifact transfer, database migration, systemd action or production writes.
The first deployment after successful provisioning remains the ordinary
reviewed `dev` push/automatic chain.

Evidence is never stored under `HOST_TOOLS_ROOT` or a generation directory.
`INSTALL_EVIDENCE` must be a newly-created fixed handoff filename in a
root-owned, non-symlink, mode-0700 secure directory (a root-owned sticky parent
such as `/tmp` is allowed). The file is opened with `O_EXCL|O_NOFOLLOW`, fully
written, fsynced and rechecked by pathname/device/inode/link-count/mode/owner;
an existing file, symlink, hardlink, special file, partial write, missing
primitive or cross-device stage is a hard failure. Parent directories are
fsynced after unlink. Signals, `KeyboardInterrupt` and `SystemExit` retain
their original exception and never trigger broad quarantine. `current`,
`previous` and all pre-existing generations are outside the cleanup identity
set.

The trusted-root threat model is explicit: the reviewed helper and the
independently supplied receipt digest are trusted inputs; artifact bytes,
artifact paths, receipt contents, handoff paths and host-directory entries are
attacker-controlled. The helper therefore rejects unavailable
`O_DIRECTORY`/`O_CLOEXEC`/`O_NOFOLLOW`/`O_EXCL`, `dir_fd`, `pread`, `fchmod`,
`fchown`, `fsync` or `renameat2(RENAME_NOREPLACE)` semantics rather than
falling back to weaker operations. After byte/provenance verification it runs
exact `/usr/bin/python3.12 -I -B` capability and host-contract self-tests in
that new generation, then repeats the closed inventory check; failure removes
only that new generation when its identity remains provable.

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
