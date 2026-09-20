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
