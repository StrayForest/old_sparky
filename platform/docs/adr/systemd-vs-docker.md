# ADR: Keep systemd and Atomic Releases

- Status: Accepted
- Date: 2026-08-01

## Decision

Keep Nginx, systemd services, shared runtimes and symlinked immutable releases.
Do not migrate the launch to Docker.

## Rationale

The current deployment already has bounded systemd units, production smoke,
restore-verified backups, release retention and tested symlink rollback.
Containerization would change packaging, persistence, networking, observability
and rollback simultaneously without solving a measured launch blocker.

Revisit only through a separate ADR with a migration/rollback drill and an
identified operational benefit.
