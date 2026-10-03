# Candidate-bound forward migration guard

- Status: Accepted
- Owner: Platform release maintainers
- Last reviewed: 2026-10-04

## Context

Production rollback restores release files and services but does not downgrade
the database. The active release can therefore have an Alembic graph older
than a fully authenticated candidate while the database already records a
revision introduced by that candidate. Comparing the database only with the
active release blocks a safe forward deployment before the candidate graph is
available.

## Decision

Deployment preflights continue to enforce every operational check, including
environment, configuration, database connectivity, edge policy and verified
backup. Only the active-release `current`/`heads` equality check is deferred
inside the authenticated deploy path. Operator `mode=preflight` and
post-activation preflight retain strict active-release equality.

After artifact provenance and transaction identity are validated, the
candidate's matching shared Python runtime reads the candidate Alembic graph
and the live `platformdb` revision registry under the existing release locks,
after application writers stop and before recovery helpers or Alembic writes.
The registry must be the unique regular `alembic_version` relation resolved by
the connection in the `platform` or `public` schema. The candidate graph must
have one head. A normal upgrade requires exactly one database revision that
is known to and is an ancestor of that head, including the head itself. An
empty registry is allowed only for a first-install transaction with both
release pointers absent. Unknown, multiple or divergent revisions, ambiguous
registries, and multi-head graphs fail closed.

The guard runs read-only with bounded database timeouts. On success the
existing partial-migration recovery helper and unchanged `alembic upgrade
head` path run under the same lock and transaction authority. A retained
pending operation still requires its exact existing resume/abort authority.
No path stamps, downgrades, skips a revision or deletes a retained receipt.

## Consequences

A candidate whose sole graph head is already recorded can complete the
ordinary forward release without changing the database. A known ancestor can
run the existing forward migrations. If validation or migration fails, the
transaction remains available for the documented recovery path; filesystem
rollback never reverses the database. See the [deployment runbook](../deployment-runbook.md)
and [release state machine](../release-state-machine.md).
