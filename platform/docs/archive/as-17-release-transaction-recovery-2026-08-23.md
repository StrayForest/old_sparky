# AS-17 End-to-end release transaction and recovery — closed

- Status: Archived / resolved
- Closed: 2026-08-23
- Implementation commit: `356d7832d480fb6557d1b3692823c3b036dffb43`
- Production deployment run: `32634067684`
- Production deployment: [GitHub Actions run](https://github.com/StrayForest/old_sparky/actions/runs/32634067684)
- Verified production release: `gha-32634067684-1-356d7832d480-20260823T103422Z`
- Previous release retained: `gha-32631736423-1-f3b85310e553-20260823T094325Z`
- Alembic head: `20260822_0040`; no automatic downgrade was added

## Original finding

The durable release receipt did not cover three crash/configuration boundaries:
resume from `activation-committed` could report success while retaining the
receipt; a crash after candidate Nginx mutation could restore pointers and
services while leaving candidate Nginx active; and ordinary rollback switched
code and venv without reinstalling the previous release's units and Nginx.

## Remediation delivered

The state machine adds `nginx-pending` and retains the receipt through an
explicit runtime recovery phase. Resume from `activation-committed` now calls
the final completion idempotently. Abort recovery keeps the receipt while it
restores the recorded filesystem state, release-specific systemd units and
Nginx, then restarts/readiness-smokes before receipt cleanup.

Rollback now records `rollback-runtime-pending`, installs the previous
release's units and Nginx before restart/smoke, and retains enough state to
either finish an interrupted restart-pending rollback or restore the exact
pre-rollback pointers, venv, units and Nginx. The transaction schema remains
expand-compatible; no Alembic downgrade path was introduced.

## Verification and deployment

The pre-fix regression scenarios reproduced all three reported failures on the
original HEAD. The focused release suite then passed with fault injection after
pointers, venv, units, Nginx, restart, smoke, activation commit and final
receipt cleanup. The full platform test gate, docs check, shell/Python syntax
checks and secret scan passed.

Production run `32634067684` passed preflight, immutable build/checksum,
release deployment, service/unit preparation, Nginx dry-run/apply, origin and
public deployment smoke, and the success deployment status. The live release
was `gha-32634067684-1-356d7832d480-20260823T103422Z`; the previous release
remains installed and is the explicit rollback target.

## Retained invariant

The receipt is the authority for an in-flight release operation. No unrelated
install may proceed while it exists, and migration uncertainty remains an
operator decision point. Rollback changes code/runtime configuration only and
never reverses Alembic automatically.
