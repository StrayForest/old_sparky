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
reading production secrets or opening SSH.

The host installs one root-owned, immutable, content-addressed generation
under `shared/.release-recovery/generations/<bundle-sha256>`. Its fixed
entrypoint executes only `abort_retained_only`, accepts a v2
`recovery-restored` install receipt, restores the recorded runtime, verifies
the retained systemd receipt and calls `complete-recovery`. It never checks
out source, runs Alembic, downgrades a migration or selects a normal deploy
entrypoint.

## Consequences

- Bootstrap changes receive full deterministic CI but are always
  `deployable=false`; mixed application/runtime changes retain normal route
  classification.
- Missing, uncertain, tampered or migration-uncertain receipts remain
  retained. Any helper, restore, verification or completion failure leaves
  the receipt in place.
- Host recovery remains available even when the active release cannot safely
  interpret the newer transaction contract.
