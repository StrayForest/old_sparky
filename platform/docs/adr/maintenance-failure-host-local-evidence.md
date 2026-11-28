# Host-local maintenance failure evidence and current-state acknowledgement

- Status: Accepted
- Date: 2026-11-28
- Owners: Platform release maintainers
- Supersedes: [encrypted evidence transport](maintenance-failure-evidence-encryption.md)

## Context

The maintenance capture already stores bounded raw diagnostics on the host
under a root-owned mode-`0700` directory with mode-`0600` files. Sending the
same evidence to an operator was implemented through CMS and required an
operator-held certificate and private key. That prerequisite prevented the
maintenance recovery procedure from being used by its intended operator.
The previous failure marker also does not identify which lock was held; a
timestamp overlap cannot establish historical lock ownership.

## Decision

Keep raw service properties, transaction receipts, queued jobs, cgroup
membership and bounded journal evidence on the production host. Capture uses
the existing O_EXCL source/run-named file and never transfers the raw file or
command output to Actions. The capture action publishes one closed
`maintenance_failure_receipt` artifact containing the exact repository,
workflow, source, bundle, capture/security/recovery/publisher run tuples, the
raw evidence SHA-256, `host_bound=true`, and `historical_cause=unknown`. The
artifact contains no host identity, paths, PIDs, cgroup contents, journal,
exception text, or stdout/stderr. Logs contain only fixed status markers.

For `reset_maintenance_failed`, the operator supplies the exact successful
capture run ID and attempt, but no certificate, private key or evidence hash.
Before SSH, the workflow validates the exact run metadata and artifact API
identity/digest, one-member archive, closed receipt schema, every source/run
binding and the evidence hash format. The host recomputes that hash against
the root-local evidence and validates its metadata, schema, host identity,
release pointers, transaction receipts, and exact failed service invocation.
Under the canonical release lock, the helper rechecks service properties,
timer properties, queued jobs and cgroup membership immediately before the
single permitted `systemctl reset-failed deadlock-maintenance.service` call.
If the timer was active and enabled, it pauses the timer for this operation
and restores the same state. It never starts maintenance or changes release,
database or configuration state. Evidence remains for separate review or
disposal.

The host keeps a root-only durable acknowledgement record bound to the exact
capture, digest, source, release tuple and original timer/service snapshots.
It records `prepared`, `reset_pending`, `unit_reset` and `complete` phases so an
interrupted response can restore the timer or confirm systemd's exact
post-reset state without repeating a completed reset or accepting a new
invocation. If `reset_pending` still has the original failed state, the helper
restores the timer and refuses another reset; a new capture is required before
trying again. A completed action can be replayed only while its post-reset
service state and restored timer still match.

This route is a current-state acknowledgement, not a repair or retrospective
cause claim. The historical cause remains unknown unless independently
reviewed evidence supports it. Reset success receives no maintenance or QA
credit; a current strict readiness check and a fresh installed-unit-bound
retention preview are separate postconditions. Any failed guard preserves the
evidence and stops.

## Consequences

- The operator workflow no longer needs a certificate, private key or local
  decrypt step for maintenance failure recovery.
- GitHub receives only a provenance-bound digest receipt; raw diagnostic
  content stays on the host.
- The receipt digest binds the exact host file to the selected capture run and
  is rechecked by the host. Reset refuses changed bytes, host, invocation,
  pointers, receipts or active work.
- Historical lock attribution may remain unavailable. The workflow reports
  `unknown` instead of inferring an owner from timing.
