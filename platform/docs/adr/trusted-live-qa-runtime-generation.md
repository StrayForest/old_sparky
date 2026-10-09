# Digest-bound trusted live-QA runtime generation

- Status: Accepted
- Date: 2026-09-19
- Owners: Platform maintainers
- Supersedes: [Secret-job artifact boundary](secret-job-artifact-boundary.md)

## Context

The credential-bearing live-user QA path must not execute a candidate checkout,
host-installed mutable dependency tree or an arbitrary path. A release also
needs a deterministic browser runtime without carrying the application's full
`node_modules` tree or any secret-bearing generated output.

## Decision

Every release artifact must contain a reviewed `liveqa-runtime` member. The
builder copies only the pinned Node executable, lock-installed Playwright test,
Playwright and Playwright Core packages, reviewed live-QA sources, and
checksum-pinned browser archives. Its runtime manifest records the package-lock
and complete content digests; artifact validation rejects missing, extra,
symlinked or special runtime members.

Activation, rollback and recovery reconcile that runtime under the canonical
release lock into `/root/.oldsparky/liveqa/releases/<source-sha>`. A root-owned
active manifest and relative generation pointer are fsynced and switched only
after the complete generation is durable. The fixed helper, dispatcher and
mailbox helper are digest-bound to the same manifest. A partial switch fails
closed, and retention removes only inactive generations after exact
active/current/previous identity checks.

The trusted parent `/root/.oldsparky/liveqa` remains root-private at mode
`0700`; its `releases` payload root is root-owned mode `0755` to satisfy the
canonical generation metadata validator while preserving the parent access
boundary. Installation creates that root at the exact mode even when the
caller's umask is restrictive. For compatibility, only an existing root-owned
`0700` payload root is normalized to `0755`; other unexpected metadata fails
closed. This adjustment does not change existing payload bytes or recursively
change generation permissions.

The copied remote dispatcher validates the active `RELEASE.json` by loading
the `platform_validate_release_artifact.py` sibling beside its trusted-root
copy. The installer copies that validator from the immutable generation and
binds the sibling to its payload-manifest digest before switching the active
pointer. The sibling is root-owned, non-writable and executable (mode `0555`);
a missing or altered payload validator or trusted-root copy fails closed.

The only set-id file permitted anywhere in this contour is the root-owned
Chromium sandbox at its exact reviewed path, mode `04755` and pinned SHA-256.
The guard, safe environment and dispatcher accept only the active manifest's
absolute payload path; a checkout or caller-supplied alternative is invalid.

## Consequences

- A first deployment remains unavailable until its artifact contains the
  runtime and activation completes, but it is installable without a host-side
  npm/browser build.
- The host still provisions the unprivileged `oldsparky-liveqa` identity and
  AppArmor profiles; it does not provision a mutable Playwright dependency
  tree.
- A failed or interrupted activation may retain an inactive generation for
  guarded recovery, but no secret bundle is copied into a release generation.
