# AS-17 End-to-end release transaction and recovery — closed

- Status: Archived / resolved
- Closed: 2026-08-23
- Implementation commit: `f5189826613d4afa2d48f834d6fbf006f4c024dd`
- CI/CD gate hardening commit: `bbe10ae17c0686f3145695714f7b2dcc01a3063c`
- Security/build run: `32637809369`
- Production deployment run: `32638099084`
- Production deployment: [GitHub Actions run](https://github.com/StrayForest/old_sparky/actions/runs/32638099084)
- Verified production release: `gha-32638099084-1-bbe10ae17c06-20260823T120031Z`
- Previous release retained: `gha-32636564272-1-f5189826613d-20260823T112755Z`
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
pre-rollback pointers, venv, units and Nginx. The previous release's
transaction code is not required after the pointer switch: before switching,
rollback refreshes a root-owned shared recovery bundle and installs a
compatibility shim at the previous release's rollback entrypoint. A second
process launched through the switched `current` delegates to that bundle. The
transaction schema remains expand-compatible; no Alembic downgrade path was
introduced.

## Verification and deployment

The pre-fix regression scenarios reproduced all three reported failures on the
original HEAD. The focused release suite then passed with fault injection after
pointers, venv, units, Nginx, restart, smoke, activation commit and final
receipt cleanup. The two-process regression also kills rollback after pointer
switch, invokes the old `current/tools/platform_release_rollback.sh`, and
verifies receipt, pointers, venv, units and Nginx recovery through the shared
bundle. The final GitHub security/build run `32637809369` passed backend,
migration, web, web-hermetic, documentation, static/security and aggregate
status gates. Server-side media/content diagnostics for the exact SHA also
passed in runs `32637809378` and `32637809347`.

Production run `32638099084` passed the fail-closed exact-SHA security gate,
preflight, immutable build/checksum,
release deployment, service/unit preparation, Nginx dry-run/apply, origin and
public deployment smoke, and the success deployment status. Live checks showed
active API/worker/web/Nginx services, API and web readiness `200`, successful
Nginx syntax validation, Alembic head `20260822_0040`, and
`current`=`gha-32638099084-1-bbe10ae17c06-20260823T120031Z` with the previous
release retained as the explicit rollback target.

## Retained invariant

The receipt is the authority for an in-flight release operation. No unrelated
install may proceed while it exists, and migration uncertainty remains an
operator decision point. Rollback changes code/runtime configuration only and
never reverses Alembic automatically.
