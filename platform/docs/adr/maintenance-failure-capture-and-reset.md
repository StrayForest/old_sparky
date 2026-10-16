# Signed maintenance failure capture and reset

- Status: Accepted
- Date: 2026-10-16
- Owners: Platform release maintainers

## Context

The read-only capture probe can identify a failed owned unit but intentionally
does not expose the unit result fields or journal needed for cause review. The
production maintenance service must remain stopped while that evidence is
collected and reviewed.

## Decision

Two separate capabilities use the existing recovery-bootstrap producer,
publisher, provenance and attestation chain. Both actions accept only the
exact source-bound security, producer and publisher run/attempt tuple, validate
the immutable bundle and install one content-addressed generation under the
canonical release lock. The existing retained-abort action and its confirmation
remain unchanged in behavior.

`capture_maintenance_failure` reads only the fixed
`deadlock-maintenance.service` and `deadlock-maintenance.timer`, the complete
systemd job list, service cgroup PIDs, current/previous pointers and release
receipt identities, plus at most 100 unit journal entries/128 KiB. The evidence
contains bounded properties, receipt hashes and selected transaction fields;
it does not collect process arguments, environments, database contents or
unrelated service journals. Known credential patterns are redacted from
journal messages. The structured evidence is never printed to Actions logs.
Capture requires the maintenance timer inactive, no queued jobs and an empty
service cgroup. It writes one root-owned mode-`0600` file to the private staging
directory and uploads it only when the repository visibility is `private`;
the artifact expires after one day. Operators retain an exact mode-`0600`
download and SHA-256 in the private evidence store before expiration.

`reset_maintenance_failed` is a distinct capability. Before SSH, the workflow
revalidates the exact successful capture workflow run/attempt, source SHA,
security run/attempt, artifact ID/name/digest, closed evidence schema and
caller-supplied evidence-file SHA. The host checks the same source, bundle,
run/attempt, transaction, pointer and host identity bindings under the release
lock; it re-reads the service, timer, job list and cgroup immediately before
the one permitted command:
`systemctl reset-failed deadlock-maintenance.service`. The command cannot
start or restart a unit. Capture evidence must be reviewed before this action;
receipt drift, a changed host or any active work stops the operation.

## Verification ownership

The existing `test_platform_recovery_bootstrap` privileged owner covers the
actual evidence helper, immutable bundle inventory/capabilities, workflow
artifact binding, private repository gate, fixed-unit command and fail-closed
receipt drift. The reset action remains gated by fresh signed-source and
evidence pins; production execution is not part of local tests.
