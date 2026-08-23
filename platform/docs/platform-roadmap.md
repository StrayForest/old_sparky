# Platform roadmap

- Status: Active backlog and priority map
- Owner: Platform maintainers
- Last reviewed: 2026-08-23

For the production baseline and immediate engineering target, read [`CURRENT.md`](CURRENT.md). This file deliberately omits release diaries and detailed audit evidence.

## P1 — security and correctness

### AS-17 — End-to-end release transaction and recovery

**Implementation in progress.** The release path now stages a candidate and
retains durable phases through migration, pointer activation, restart/readiness,
Nginx apply and smoke. Migration-uncertain phases fail closed and require
operator resume/rollback review; automatic Alembic downgrade is not introduced.
The production workflow is manual-dispatch only until live proof and signed
artifact gates are complete. Closure requires a VPS interruption/recovery drill
and evidence that no failed post-install step leaves an untracked pointer,
runtime, service or Nginx state.

### AS-15 — Deadlock persistence and workflow concurrency integrity

**Resolved and deployed.** The 2026-08-22 persistence audit confirmed that
AS-03 invite/capacity serialization did not cover all Deadlock writers. AS-15
now locks and revalidates every durable ready-check, captain, assignment,
publish, roster-lock and profile-slot writer; migration `20260822_0040` adds
the cardinality and value guards, normalizes legacy visibility, and recovers
interrupted concurrent-index builds. Independent-session tests assert the
resulting stored state. Closure evidence is retained in
[`archive/as-15-deadlock-workflow-integrity.md`](archive/as-15-deadlock-workflow-integrity.md).

AS-03 remains resolved for its documented scope: invite claim/revoke and
active-participant capacity. Its archived evidence must not be cited as proof
of AS-15 workflow correctness.

AS-02 privileged-route Access/MFA is resolved with operator/dashboard/live evidence for `/platform-ops*` and `/api/v1/admin*`; application RBAC remains authoritative. Closure evidence is retained in [`archive/as-02-cloudflare-access-mfa.md`](archive/as-02-cloudflare-access-mfa.md).

AS-03 tournament invite/capacity concurrency is resolved and archived with implementation, concurrency-test and production-deployment evidence in [`archive/as-03-tournament-write-serialization.md`](archive/as-03-tournament-write-serialization.md).

AS-05 public/private data-boundary work is resolved and archived with audit, public-contract tests and production-deployment evidence in [`archive/as-05-public-private-data-boundary.md`](archive/as-05-public-private-data-boundary.md).

AS-06 SSE connection pressure is resolved and archived with layered application/Nginx limits, lease-release/crash-expiry regression coverage and production-deployment evidence in [`archive/as-06-sse-connection-pressure.md`](archive/as-06-sse-connection-pressure.md).

AS-07 R2/CDN runtime media cleanup is resolved and archived with migration inventory/reconciliation evidence, regression coverage, CI verification and production deployment in [`archive/as-07-r2-cdn-runtime-cleanup.md`](archive/as-07-r2-cdn-runtime-cleanup.md). Physical removal of runtime-inert legacy URL columns and migration-only helpers remains a separate post-grace cleanup.

## P2 — hardening and cleanup

AS-08 unknown-patch refresh hardening is resolved and archived with negative-cache/coalescing regressions, redirect/response bounds, CI verification and production deployment in [`archive/as-08-patch-miss-hardening.md`](archive/as-08-patch-miss-hardening.md).

AS-09 distributed login guessing protection is resolved and archived with account-wide/per-IP throttling regressions, bounded cooldown behavior, CI verification and production deployment in [`archive/as-09-distributed-login-guessing.md`](archive/as-09-distributed-login-guessing.md).

AS-11 public worker-error sanitization is resolved with a persistence-boundary guard, irreversible historical-data cleanup migration and a public-response regression proving arbitrary exception text cannot leave the API. Closure evidence is retained in [`archive/as-11-worker-error-sanitization.md`](archive/as-11-worker-error-sanitization.md).

- **AS-12:** code-side fail-closed bind/proxy validation and a read-only
  Cloudflare/Nginx/UFW parity gate are implemented; live listener, UFW, Nginx
  and direct-origin evidence remains required before closure.
- **AS-13:** CI now creates the actual web/api/worker identities, installs the
  current systemd units and exercises the runtime env boundary; retain the
  workflow result as revalidation evidence before closure.
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
- a completed release transaction receipt with explicit recovery outcome; a
  post-migration failure is not silently marked rolled back.

Detailed active findings belong in [`application-security-audit.md`](application-security-audit.md). Historical findings and completed remediation evidence belong in [`archive/`](archive/). CSP policy/operations belong in [`security-runbook.md`](security-runbook.md). Release commands and rollback procedure belong in [`deployment-runbook.md`](deployment-runbook.md).
