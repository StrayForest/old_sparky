# Immutable recovery bootstrap for retained release abort

- Status: Accepted
- Date: 2026-09-27
- Owners: Platform maintainers

## Context

A retained transaction can leave `current` pointing at an older or partially
compatible release. Selecting recovery code from that release or checking
out source on the production host is not a dependable recovery authority.
Migration uncertainty remains irreversible and must stay fail-closed.

## Decision

The trusted default-branch security run produces a deterministic,
non-deployable bundle containing only the closed recovery-helper closure. Its
manifest binds source SHA, exact security run/attempt, artifact digest, modes
and member digests. The manual recovery-bootstrap abort workflow validates the
successful run, route artifact, evidence, bundle digest and attestation before
reading production secrets or opening SSH. Because the GitHub certificate's
`runInvocationURI` identifies a run/attempt rather than a job, the exact
recovery job ID is selected from that attempt's jobs API, emitted by the build
evidence, and cross-checked as a closed evidence/API pair; it is never inferred
from or fabricated into the certificate URI.

The host installs one root-owned, immutable, content-addressed generation
under `shared/.release-recovery/generations/<bundle-sha256>`. Its manifest
advertises the two closed capabilities `abort_retained_only` and
`recover_pending`; the fixed abort entrypoint executes only
`abort_retained_only`, accepts a v2
`recovery-restored` install receipt, restores the recorded runtime, verifies
the retained systemd receipt, and performs two-phase completion: it removes
the candidate/venv cleanup artifacts with `complete-recovery
--retain-receipt`, clears the systemd receipt, and only then removes the
operation receipt with a final `complete-recovery`. A retry after either
durable cleanup side effect is idempotent and fails closed if the receipt pair
or cleanup identities are inconsistent. It never checks out source, runs
Alembic, downgrades a migration or selects a normal deploy entrypoint.

The normal release-recover entrypoint has an explicit topology split but one
immutable control plane. It transfers or reuses the exact attested bundle
whose SHA is bound to the security-run/recovery-run evidence, installs it
under that content address, validates its closed manifest and requested
`recover_pending` capability, and invokes only
`platform_recover_pending.sh`. For first-install and current-only receipts it
runs only the generation's lock/transaction helpers and requires no systemd
receipt; it never sources `current/tools`. Upgrade and rollback use that same
generation wrapper for the two-pointer systemd contract, while release-specific
data-plane helpers are accepted only through the systemd manifest bound to the
recorded release.

For an operation-ID rollback, `platform_recover_pending.sh` owns the complete
phase matrix inside that generation: pre-runtime phases retain the filesystem
transaction, runtime-pending phases restore the original runtime and bound
systemd receipt, `restart-pending` resumes the swapped target, and later
phases only verify and complete the two-phase cleanup. A retry after the
systemd receipt has been cleared consumes the already-proven transaction phase
without querying systemd. No rollback recovery path executes a helper from
`current/tools` or invokes the release's rollback shim; release-specific
runtime files are data-plane inputs whose immutable receipt manifest is
revalidated before use.

New transaction receipts carry one immutable 32-hex `operation_id`; the
systemd receipt must carry the same ID and the canonical current/previous
release paths and inode identities before any runtime helper, `systemctl`, or
receipt unlink. Its helper manifest covers both rollback targets, including
the live-QA runtime installer, and is checked against the release actually
passed to runtime restoration. The only compatibility bridge is the exact
legacy v2 `install`/`recovery-restored` receipt with no systemd receipt: it
performs receipt-owned candidate cleanup only, never synthesizes an ID or
executes a retained release helper. Missing, mismatched, or present systemd
state fails closed.

The trusted workflow also rejects duplicate or unsafe outer/inner ZIP members,
bounded-size/compression violations, symlink/special/non-regular entries, and
unsafe staging directories. Recovery staging cleanup reports both the primary
operation and cleanup result; a cleanup failure cannot turn a failed recovery
into success.

## Consequences

- Bootstrap changes receive full deterministic CI but are always
  `deployable=false`; mixed application/runtime changes retain normal route
  classification.
- Missing, uncertain, tampered or migration-uncertain receipts remain
  retained. Any helper, restore, verification or completion failure leaves
  the receipt in place.
- Host recovery remains available even when the active release cannot safely
  interpret the newer transaction contract.
