# Host-tools bundle attestation verification

Use this procedure with the admitted provisioning route in the
[host-tools provisioning ADR](production-host-tools-provisioning.md#one-time-operator-provisioning-out-of-band).
It verifies provenance; it does not replace the offline bundle/content checks
or authorize publication by itself.

## Prepare a supported verifier

Before route (a), dispatch its read-only `mode=preflight` run. Before route
(b), prepare this verifier before the source-bound backup/AUTO readiness
window; do not open the 1,800-second window until it is ready. Route (c) uses
completed child evidence and does not open a new AUTO window. If system `gh`
does not support `gh attestation verify --help`, use the official isolated
v2.105.0 binary below; never skip verification or change system packages.

The GitHub CLI Linux amd64 archive must match the asset digest in the official
v2.105.0 release API response. The response is retrieved over HTTPS but is not
a detached signature. Stop if the asset name/digest or downloaded archive hash
does not match the pinned value.

```sh
set -euo pipefail
umask 077
GH_TMP="$(mktemp -d /tmp/platform-gh-attestation.XXXXXX)"
trap 'rm -rf -- "$GH_TMP"' EXIT
API_JSON="$GH_TMP/release.json"
/usr/bin/curl --fail --location --silent --show-error \
  --output "$API_JSON" \
  https://api.github.com/repos/cli/cli/releases/tags/v2.105.0
/usr/bin/python3.12 -I -B - "$API_JSON" <<'PY'
import json
import sys
release = json.load(open(sys.argv[1], encoding="utf-8"))
matches = [asset.get("digest") for asset in release["assets"]
           if asset.get("name") == "gh_2.105.0_linux_amd64.tar.gz"]
if matches != ["sha256:79e7ab5db738c48777b696e374f8b428f0647debfb424f44d2f426370407fe2f"]:
    raise SystemExit("official GitHub CLI release asset digest mismatch")
PY
ARCHIVE="$GH_TMP/gh_2.105.0_linux_amd64.tar.gz"
/usr/bin/curl --fail --location --silent --show-error \
  --output "$ARCHIVE" \
  https://github.com/cli/cli/releases/download/v2.105.0/gh_2.105.0_linux_amd64.tar.gz
printf '%s  %s\n' \
  79e7ab5db738c48777b696e374f8b428f0647debfb424f44d2f426370407fe2f \
  "$ARCHIVE" | /usr/bin/sha256sum --check -
/usr/bin/tar -xzf "$ARCHIVE" -C "$GH_TMP" \
  --no-same-owner --no-same-permissions --strip-components=1 \
  gh_2.105.0_linux_amd64/bin/gh
GH="$GH_TMP/bin/gh"
test -x "$GH"
"$GH" version | /usr/bin/grep -q '^gh version 2.105.0 '
"$GH" attestation verify --help >/dev/null
```

Keep the mode-0700 temporary directory, `GH`, and cleanup trap in scope for
attestation verification below. The trap removes only this exact temporary
directory on exit.

## Verify the selected artifact

Use exact run/attempt and artifact receipts from the admitted route. Keep the
identities separate: `HOST_TOOLS_SHA` (**C**) names the bundle's source code;
`TARGET_SHA` (**Q**) is the source/ref that invoked the builder; the attestation
subject digest is the downloaded bundle SHA-256. The parent's artifact
ID/name/digest must bind to its exact run/attempt, independent of Security.
Download through the approved channel and verify the outer SHA-256 before
opening the bundle. Do not use a pull-request candidate artifact.

Create a fresh JSON file inside `GH_TMP` and require `gh` exit 0, exactly one
verification row, and a nonempty `verifiedTimestamps` list. Reject absent,
duplicate, ambiguous or mismatched fields. The expected paths and values are:

- `rows[0].verificationResult.statement.subject` is exactly one object naming
  `platform-host-tools-bundle.zip` with the downloaded bundle SHA-256. This
  digest is neither **C** nor **Q**.
- `rows[0].verificationResult.statement.predicateType` is
  `https://slsa.dev/provenance/v1`.
- `rows[0].verificationResult.signature.certificate.sourceRepositoryURI`,
  `.sourceRepositoryRef` and `.sourceRepositoryDigest`, plus
  `rows[0].verificationResult.statement.predicate.buildDefinition.resolvedDependencies[0].digest.gitCommit`,
  match the admitted repository, ref and **Q**. Current-`dev` routes require
  exactly one dependency URI
  `git+https://github.com/StrayForest/old_sparky@refs/heads/dev` with commit
  **Q**.
- Certificate fields
  `rows[0].verificationResult.signature.certificate.githubWorkflowRepository`,
  `.githubWorkflowName`, `.githubWorkflowRef`, `.githubWorkflowSHA` and
  `.githubWorkflowTrigger`, and statement fields
  `rows[0].verificationResult.statement.predicate.buildDefinition.externalParameters.workflow.repository`,
  `.path` and `.ref`, match the exact admitted workflow.
- Certificate field
  `rows[0].verificationResult.signature.certificate.runInvocationURI` and
  statement field
  `rows[0].verificationResult.statement.predicate.runDetails.metadata.invocationId`
  both equal
  `https://github.com/StrayForest/old_sparky/actions/runs/<run_id>/attempts/<run_attempt>`.
  Bind the attempt as part of this URI. Route (c) binds its child failure
  run/attempt separately from the AUTO parent invocation.

The route bindings are:

- (a) `.github/workflows/platform-production-deploy.yml`, exact read-only
  preflight run/attempt and successful `build-host-tools` job.
- (b) `.github/workflows/platform-production-autodeploy.yml`, exact AUTO
  parent run/attempt and successful builder job.
- (c) The AUTO parent that produced the bundle plus the separate completed
  child run/attempt whose only failure was the missing pinned generation
  before build.

For route (a), a pre-install preflight may be overall unsuccessful only when
its exact host-capability gate reports pinned **C** absent before build,
transaction, transfer or protected write. The exact attempt must still have a
successful builder job, and the attestation above must verify every route
binding. The dispatch-input artifact must prove `mode=preflight` before jobs
are interpreted. The exact workflow's permitted skipped jobs are
`Validate exact classifier artifact before build` (not applicable in
preflight mode), plus `Authenticate active production baseline and cumulative
route`, `Publish exact non-runtime no-op source receipt`,
`Authenticate exact report-only failed release and active baseline`,
`Dispatch exact-target baseline runtime proof`,
`Validate exact-target baseline runtime proof`,
`Build immutable release artifact in CI`, `Validate exact security
provenance`, `Production preflight`, `Deploy production`, and
`Dispatch automatic post-deploy live QA` (mode/dependency gated in this
preflight route). The maintained operator accepts only this closed skip-name
set; any other skipped job, any other failed step, ambiguous binding or
incomplete required check makes the artifact ineligible.
After provisioning **C**, a separate fresh exact-target
`mode=preflight` must pass in full, including installed host capability.

Set `SIGNER_WORKFLOW` to `StrayForest/old_sparky/` plus the route's workflow
path; derive `SOURCE_REF`, **Q**, run ID/attempt, **C** and bundle digest from
the exact receipts. Do not substitute a child invocation for the signer or
reuse identities from an earlier run.

## Final publication checks

Immediately before publication, revalidate that `TARGET_SHA` is the reviewed
current `dev` head, the exact pin selects `HOST_TOOLS_SHA`, attestation and
artifact identities remain route-bound, and no release transaction or
competing production operation exists. Hold the canonical release lock then
the LiveQA lock through final inventory recheck, no-replace publication and
fixed installed self-test. Route (b) additionally requires the same AUTO
readiness job and shared verifier step still `in_progress` with dispatch not
started; stop if its 1,800-second window expired or readiness completed. Do
not reuse that artifact outside the window. Route (a) revalidates the exact
read-only `mode=preflight` run/attempt at current merged `dev`, successful
builder job and sole eligible missing-**C** refusal above. Its post-install
preflight is a separate run and must pass in full.
Route (c) revalidates the exact completed child failure: its only failure was
the missing pinned generation before build, pending state or production write.
That child is not an active AUTO window. Every route preserves the builder job,
artifact ID/name/digest, **C**, inventory and source/run/attempt bindings. Stop
without publication on stale, ambiguous or changed evidence.

```sh
ATTESTATION_JSON="$(mktemp "$GH_TMP/attestation.XXXXXX")"
"$GH" attestation verify "$BUNDLE" \
  --repo StrayForest/old_sparky \
  --signer-workflow "$SIGNER_WORKFLOW" \
  --source-ref "$SOURCE_REF" \
  --source-digest "$TARGET_SHA" \
  --format json > "$ATTESTATION_JSON"
```

Validate the returned JSON against every closed field and route binding above;
command success alone is insufficient. Keep the JSON private and do not upload
or commit it. The offline `platform_host_tools_bundle.py verify` and outer
SHA-256 remain separate content checks; retain their outputs with the
provisioning record. Remove the exact temporary directory after evidence is
recorded. A verifier failure aborts provisioning and cannot be retried with
weaker checks.
