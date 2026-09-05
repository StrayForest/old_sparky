# Platform roadmap

- Status: Active backlog and priority map
- Owner: Platform maintainers
- Last reviewed: 2026-09-01

For the production baseline and immediate engineering target, read [`CURRENT.md`](CURRENT.md). This file deliberately omits release diaries and detailed audit evidence.

## P1 — security and correctness

### AS-18 — Hot-path capacity and backpressure

**Resolved.** Participant slots, idempotent join, non-locking ordinary ready
votes, conditional ETag reads, bounded pools and priority queues are
implemented and deployed. The package covers join capacity slots,
compact conditional workflow reads, bounded API/worker pools, Celery
priority/backpressure and retained load evidence. Existing 128-shard ready
votes and PostgreSQL workflow authority remain in force. Ready Check uses the
server-known timing contract, and passive grid changes are visible after
manual reload. Ready Vote additionally uses a process-local adaptive
admission boundary and bounded browser retries; production Pareto evidence
for the unchanged 15k/20k external workloads remains an operator gate.

### AS-17 — End-to-end release transaction and recovery

**Resolved and deployed.** The release state machine now has an explicit
`nginx-pending` uncertainty boundary, idempotent resume from
`activation-committed`, and retained recovery that restores release-specific
units and Nginx before restart/smoke. Rollback restores the previous code,
venv, units and Nginx, and a shared recovery bundle plus previous-release shim
keeps that recovery available after `current` switches. Migration uncertainty
remains fail-closed and no automatic Alembic downgrade is introduced.
Fault-injection regression tests cover every requested side-effect boundary;
closure evidence is retained in
[`archive/as-17-release-transaction-recovery-2026-08-23.md`](archive/as-17-release-transaction-recovery-2026-08-23.md).

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


AS-07 R2/CDN runtime media cleanup is resolved and archived with migration inventory/reconciliation evidence, regression coverage, CI verification and production deployment in [`archive/as-07-r2-cdn-runtime-cleanup.md`](archive/as-07-r2-cdn-runtime-cleanup.md). Runtime call-site plumbing is removed; persisted legacy URL fields and migration-only helpers remain only for compatibility and existing-data reconciliation until a production data/consumer inventory supports a reviewed API/schema migration.

## P2 — hardening and cleanup

AS-08 unknown-patch refresh hardening is resolved and archived with negative-cache/coalescing regressions, redirect/response bounds, CI verification and production deployment in [`archive/as-08-patch-miss-hardening.md`](archive/as-08-patch-miss-hardening.md).

AS-09 distributed login guessing protection is resolved and archived with account-wide/per-IP throttling regressions, bounded cooldown behavior, CI verification and production deployment in [`archive/as-09-distributed-login-guessing.md`](archive/as-09-distributed-login-guessing.md).

AS-11 public worker-error sanitization is resolved with a persistence-boundary guard, irreversible historical-data cleanup migration and a public-response regression proving arbitrary exception text cannot leave the API. Closure evidence is retained in [`archive/as-11-worker-error-sanitization.md`](archive/as-11-worker-error-sanitization.md).

- **AS-12 closed 2026-09-05:** the exact-SHA read-only production proof passed
  listener inventory, forwarded-header trust, Cloudflare/Nginx/UFW parity and
  external IPv4/IPv6 direct-origin blocking. Evidence is retained in
  [`archive/as-12-origin-perimeter-2026-09-05.md`](archive/as-12-origin-perimeter-2026-09-05.md).
- **AS-13 closed 2026-09-01:** the exact-SHA security/build workflow passed
  with the current web/api/worker identities, systemd units, runtime env
  boundary and isolated test database. Evidence is retained in
  [`archive/application-security-as-13-2026-09-01.md`](archive/application-security-as-13-2026-09-01.md).
- Production-host release provenance is resolved in the current deployment
  workflow: CI builds and attests the immutable artifact and wheelhouse, while
  the VPS verifies the published digest and source commit before installation.
- After production data and external-consumer inventory confirms that no compatibility or migration path depends on them, remove the persisted legacy media URL fields and migration-only helpers through a reviewed API/schema migration.
- Continue reducing legacy/duplicate runtime and documentation paths after each replacement is proven.

## Operational / owner-controlled

- AS-14 HSTS ownership/state is resolved: Cloudflare owns visitor HSTS with a six-month max-age, `includeSubDomains` Off and preload Off. Evidence is retained in [`archive/as-14-cloudflare-hsts-ownership.md`](archive/as-14-cloudflare-hsts-ownership.md).
- Cloudflare Full(strict), minimum TLS 1.2, TLS 1.3/HTTP3 and DNSSEC were operator-confirmed on 2026-08-21.
- Verify remaining Cloudflare certificate alerts, CAA, R2 public-bucket/media-token settings, WAF/rates, Turnstile hostnames, Bot Fight Mode and Cloudflare-range/UFW parity with direct dashboard/operator evidence where still marked `VERIFY`/`TODO` in the production checklist. The public catalog cache behavior is live-proven; see [`archive/as-02-cloudflare-catalog-cache-2026-09-05.md`](archive/as-02-cloudflare-catalog-cache-2026-09-05.md).
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
