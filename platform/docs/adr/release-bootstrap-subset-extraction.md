# ADR: Validate full release archives before subset bootstrap extraction

Status: accepted for source implementation; production activation requires the normal signed host-tools provisioning and deployment chain

## Context

The production supervisor first extracts trusted release-control files to a
temporary bootstrap tree, then invokes the candidate deploy helper to install
the complete immutable release. Extracting the archive's wheelhouse and
standalone web bundle twice creates a temporary storage peak without helping
the bootstrap helper run. The archive remains a single checksum-bound
artifact, and candidate source must not gain authority before its identity and
layout have been validated.

## Decision

The bootstrap validator checks the complete archive structure and member
metadata, required release files, Live QA runtime manifest and `RELEASE.json`.
Before it writes any member, it also requires the embedded source commit to
match the supervisor's target SHA. Bootstrap mode then omits only the exact
release-root `wheelhouse/` tree and
`apps/platform_web/.next/standalone/` tree from the temporary control
extraction. The complete archive and checksum remain unchanged; the candidate
deploy helper performs the normal full release extraction later.

The [host-tools provisioning ADR](production-host-tools-provisioning.md)
records the current C2/P3 generation and owns its pin, exact closure and
provisioning history. The canonical resolver verifies the closure paths,
source modes, hashes and ancestry; this decision record owns only the
bootstrap extraction behavior.

## Consequences

The temporary bootstrap tree no longer duplicates the two bulk release
subtrees, while validation still covers the complete artifact before the
first write. The final installed release remains complete and immutable.
The pin is source evidence, not proof that production has installed this
generation; only the normal signed bundle, provisioning self-test and exact
automatic deployment chain can activate it.
