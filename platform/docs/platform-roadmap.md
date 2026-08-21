# Platform roadmap

- Status: Active backlog and priority map
- Owner: Platform maintainers
- Last reviewed: 2026-08-21

For the production baseline and immediate engineering target, read [`CURRENT.md`](CURRENT.md). This file deliberately omits release diaries and detailed audit evidence.

## P1 — security and correctness

No open P1 security/correctness item remains.

AS-02 privileged-route Access/MFA is resolved with operator/dashboard/live evidence for `/platform-ops*` and `/api/v1/admin*`; application RBAC remains authoritative. Closure evidence is retained in [`archive/as-02-cloudflare-access-mfa.md`](archive/as-02-cloudflare-access-mfa.md).

AS-03 tournament concurrency is resolved and archived with implementation, concurrency-test and production-deployment evidence in [`archive/as-03-tournament-write-serialization.md`](archive/as-03-tournament-write-serialization.md).

AS-05 public/private data-boundary work is resolved and archived with audit, public-contract tests and production-deployment evidence in [`archive/as-05-public-private-data-boundary.md`](archive/as-05-public-private-data-boundary.md).

AS-06 SSE connection pressure is resolved and archived with layered application/Nginx limits, lease-release/crash-expiry regression coverage and production-deployment evidence in [`archive/as-06-sse-connection-pressure.md`](archive/as-06-sse-connection-pressure.md).

AS-07 R2/CDN runtime media cleanup is resolved and archived with migration inventory/reconciliation evidence, regression coverage, CI verification and production deployment in [`archive/as-07-r2-cdn-runtime-cleanup.md`](archive/as-07-r2-cdn-runtime-cleanup.md). Physical removal of runtime-inert legacy URL columns and migration-only helpers remains a separate post-grace cleanup.

## P2 — hardening and cleanup

AS-08 unknown-patch refresh hardening is resolved and archived with negative-cache/coalescing regressions, redirect/response bounds, CI verification and production deployment in [`archive/as-08-patch-miss-hardening.md`](archive/as-08-patch-miss-hardening.md).

AS-09 distributed login guessing protection is resolved and archived with account-wide/per-IP throttling regressions, bounded cooldown behavior, CI verification and production deployment in [`archive/as-09-distributed-login-guessing.md`](archive/as-09-distributed-login-guessing.md).

- **AS-10 — product/security decision:** decide whether duplicate-registration UX justifies existing-email disclosure; otherwise move to a generic accepted flow with comparable timing and Turnstile behavior.
- **Next code-owned target — AS-11:** replace public worker exception text with stable public error codes/generic messages while retaining redacted diagnostics only in restricted logs/admin surfaces.
- AS-12–AS-13: proxy/firewall configuration drift and CI isolation revalidation.
- After the media grace period, remove the runtime-inert legacy media URL columns/call-site plumbing and migration-only helpers once no migration/reconciliation path depends on them.
- Continue reducing legacy/duplicate runtime and documentation paths after each replacement is proven.

## Operational / owner-controlled

- AS-14 HSTS ownership/state is resolved: Cloudflare owns visitor HSTS with a six-month max-age, `includeSubDomains` Off and preload Off. Evidence is retained in [`archive/as-14-cloudflare-hsts-ownership.md`](archive/as-14-cloudflare-hsts-ownership.md).
- Cloudflare Full(strict), minimum TLS 1.2, TLS 1.3/HTTP3 and DNSSEC were operator-confirmed on 2026-08-21.
- Verify remaining Cloudflare CAA, WAF/rates and R2 settings with direct dashboard evidence where still marked `VERIFY`/`TODO` in the production checklist.
- Classify new enforced CSP reports and perform real-user follow-up; confirmed first-party regressions use the documented rollback path rather than a widened allowlist.
- Purge/revalidate stale immutable asset metadata when required.
- Keep password-manager/browser matrix checks for password UI releases.

## Product work after security blockers

Ordinary feature expansion remains behind accepted security/correctness work unless a feature is explicitly required to fix production behavior or an accepted business priority overrides the sequence.

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
