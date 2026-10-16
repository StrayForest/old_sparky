# Signed maintenance failure capture and reset — superseded

- Status: Superseded by [encrypted evidence transport](maintenance-failure-evidence-encryption.md)
- Date: 2026-10-16
- Owners: Platform release maintainers

## Context

The read-only capture probe can identify a failed owned unit but intentionally
does not expose the unit result fields or journal needed for cause review. The
production maintenance service must remain stopped while that evidence is
collected and reviewed.

## Decision

This ADR records the original private-repository artifact boundary. Its
transport and capture preconditions are superseded. Follow the [encrypted
evidence transport ADR](maintenance-failure-evidence-encryption.md) for the
current artifact, local decryption, capture and reset requirements.

Two separate capabilities use the existing recovery-bootstrap producer,
publisher, provenance and attestation chain. Both actions accept only the
exact source-bound security, producer and publisher run/attempt tuple, validate
the immutable bundle and install one content-addressed generation under the
canonical release lock. The existing retained-abort action and its confirmation
remain unchanged in behavior.

`capture_maintenance_failure` remains a read-only bounded observation of the
fixed maintenance service/timer, transaction receipts and pointers, systemd
jobs, cgroup membership and selected unit journal. It records actual bounded
states for cause review; it does not require reset-only timer/job/process
fences before capture, print evidence to Actions logs, or inspect unrelated
application data.

`reset_maintenance_failed` is a distinct capability. Before SSH, the workflow
revalidates the exact successful capture workflow run/attempt, source SHA,
security run/attempt, artifact ID/name/digest, closed encrypted manifest and
caller-supplied locally reviewed plaintext SHA. The host checks the same
source, bundle, run/attempt, transaction, pointer and host identity bindings
under the release lock; it re-reads the service, timer, job list and cgroup
immediately before the one permitted command:
`systemctl reset-failed deadlock-maintenance.service`. The command cannot
start or restart a unit. Capture evidence must be reviewed before this action;
receipt drift, a changed host or any active work stops the operation.

## Verification ownership

The existing `test_platform_recovery_bootstrap` privileged owner covers the
actual evidence helper, immutable bundle inventory/capabilities, workflow
artifact binding, fixed-unit command and fail-closed receipt drift. Encrypted
transport and local decrypt provenance tests are described in the
superseding ADR. The reset action remains gated by fresh signed-source and
evidence pins; production execution is not part of local tests.
