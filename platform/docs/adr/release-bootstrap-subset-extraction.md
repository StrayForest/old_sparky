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

The reviewed generation is **C2**
[`382e2b93a69e0352a70feea474f33559f34d28b0`](https://github.com/StrayForest/old_sparky/commit/382e2b93a69e0352a70feea474f33559f34d28b0).
Pin-only **P3**
[`13e635a7d8cb1c5d5c00bbcffca34af1f547e28f`](https://github.com/StrayForest/old_sparky/commit/13e635a7d8cb1c5d00bbcffca34af1f547e28f)
binds the exact ordered 14-member host-tools closure to C2, including the
validator and supervisor digests. The canonical resolver proves that C2 is an
ancestor of P3 and verifies the closure paths, source modes and hashes.

## Consequences

The temporary bootstrap tree no longer duplicates the two bulk release
subtrees, while validation still covers the complete artifact before the
first write. The final installed release remains complete and immutable.
The pin is source evidence, not proof that production has installed this
generation; only the normal signed bundle, provisioning self-test and exact
automatic deployment chain can activate it.
