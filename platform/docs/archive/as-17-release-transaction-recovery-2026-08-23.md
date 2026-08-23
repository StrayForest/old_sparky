# AS-17 End-to-end release transaction and recovery — closed

- Status: Archived / resolved
- Closed: 2026-08-23
- Implementation commit: `f5189826613d4afa2d48f834d6fbf006f4c024dd`
- CI/CD gate hardening commit: `bbe10ae17c0686f3145695714f7b2dcc01a3063c`
- CI/documentation routing commit: `09574590cd80238b7441d93ef8377ddfd3b4cc07`
- Security/build run: `32638426827`
- Production deployment run: `32638711370`
- Production deployment: [GitHub Actions run](https://github.com/StrayForest/old_sparky/actions/runs/32638711370)
- Verified production release: `gha-32638711370-1-09574590cd80-20260823T121307Z`
- Previous release retained: `gha-32638099084-1-bbe10ae17c06-20260823T120031Z`
- Server-side production diagnostics: [GitHub Actions run](https://github.com/StrayForest/old_sparky/actions/runs/32639026796)
- Docs-only follow-up deployment: [GitHub Actions run](https://github.com/StrayForest/old_sparky/actions/runs/32639416463)
- Docs-only release: `gha-32639416463-1-7b7224feb13e-20260823T122756Z`
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
bundle. The final GitHub security/build run `32638426827` passed backend,
migration, web, web-hermetic, documentation, static/security and aggregate
status gates. Post-deploy server-side content diagnostics and patch translation
warm-up for the exact SHA passed in runs `32638938744` and `32638938742`; the
explicit production diagnostics workflow passed in run `32639026796`.
The docs-only follow-up deployment `32639416463` and its server-side
production diagnostics `32639662616` also passed; it did not change the
runtime implementation or migration state.

Production run `32638711370` passed the fail-closed exact-SHA security gate,
preflight, immutable build/checksum,
release deployment, service/unit preparation, Nginx dry-run/apply, origin and
public deployment smoke, and the success deployment status. Live checks showed
active API/worker/web/Nginx services, API and web readiness `200`, successful
Nginx syntax validation, Alembic head `20260822_0040`, and
`current`=`gha-32638711370-1-09574590cd80-20260823T121307Z` with the previous
release retained as the explicit rollback target.

## Retained invariant

The receipt is the authority for an in-flight release operation. No unrelated
install may proceed while it exists, and migration uncertainty remains an
operator decision point. Rollback changes code/runtime configuration only and
never reverses Alembic automatically.
