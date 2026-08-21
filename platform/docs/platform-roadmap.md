# Platform roadmap

- Status: Active backlog and priority map
- Owner: Platform maintainers
- Last reviewed: 2026-08-21

For the production baseline and immediate engineering target, read [`CURRENT.md`](CURRENT.md). This file deliberately omits release diaries and detailed audit evidence.

## P1 — security and correctness

1. **AS-02 privileged route access / MFA — operator-owned**
   - protect `/platform-ops*` and `/api/v1/admin*` with the approved Cloudflare/operator control;
   - retain application RBAC and audit events;
   - close only after direct dashboard/live evidence.

AS-03 tournament concurrency is resolved and archived with implementation, concurrency-test and production-deployment evidence in [`archive/as-03-tournament-write-serialization.md`](archive/as-03-tournament-write-serialization.md).

AS-05 public/private data-boundary work is resolved and archived with audit, public-contract tests and production-deployment evidence in [`archive/as-05-public-private-data-boundary.md`](archive/as-05-public-private-data-boundary.md).

AS-06 SSE connection pressure is resolved and archived with layered application/Nginx limits, lease-release/crash-expiry regression coverage and production-deployment evidence in [`archive/as-06-sse-connection-pressure.md`](archive/as-06-sse-connection-pressure.md).

AS-07 R2/CDN runtime media cleanup is resolved and archived with migration inventory/reconciliation evidence, regression coverage, CI verification and production deployment in [`archive/as-07-r2-cdn-runtime-cleanup.md`](archive/as-07-r2-cdn-runtime-cleanup.md). Physical removal of runtime-inert legacy URL columns and migration-only helpers remains a separate post-grace cleanup.

## P2 — hardening and cleanup

- **Next code-owned target — AS-08:** replace synchronous external refresh work for unknown public patch IDs with bounded negative caching/coalesced background refresh and validate redirect/response bounds.
- AS-09–AS-13: distributed auth counter, safe worker errors, config/CI drift and remaining audit hardening.
- After the media grace period, remove the runtime-inert legacy media URL columns/call-site plumbing and migration-only helpers once no migration/reconciliation path depends on them.
- Continue reducing legacy/duplicate runtime and documentation paths after each replacement is proven.

## Operational / owner-controlled

- Verify Cloudflare HSTS, DNSSEC, CAA, WAF/rates, edge TLS and R2 settings with direct dashboard evidence.
- Classify new enforced CSP reports and perform real-user follow-up; confirmed first-party regressions use the documented rollback path rather than a widened allowlist.
- Purge/revalidate stale immutable asset metadata when required.
- Keep password-manager/browser matrix checks for password UI releases.

## Product work after security blockers

Ordinary feature expansion remains behind the P1 security/correctness work unless a feature is explicitly required to fix production behavior or an accepted business priority overrides the sequence.

## Release gate

A production-bound package must have:

- no new Critical/direct auth bypass and no unresolved correctness blocker;
- focused tests plus applicable backend/web/security gates;
- fresh restore-verified backup and Alembic head confirmation when data risk applies;
- immutable artifact/checksum and explicit rollback target;
- healthy disk/resources;
- preflight, deploy smoke, affected live checks and clean journals;
- no automatic migration downgrade.

Detailed active findings belong in [`application-security-audit.md`](application-security-audit.md). Historical findings and completed remediation evidence belong in [`archive/`](archive/). CSP policy/operations belong in [`security-runbook.md`](security-runbook.md). Release commands and rollback procedure belong in [`deployment-runbook.md`](deployment-runbook.md).
