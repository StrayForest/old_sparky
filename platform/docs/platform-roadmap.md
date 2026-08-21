# Platform roadmap

- Status: Active backlog and priority map
- Owner: Platform maintainers
- Last reviewed: 2026-08-21

For the production baseline and immediate engineering target, read [`CURRENT.md`](CURRENT.md). This file deliberately omits release diaries and detailed audit evidence.

## P1 — security and correctness

1. **AS-04 inactive-participant authorization**
   - withdrawn, rejected, disqualified and otherwise inactive participant rows must not retain private-workspace access;
   - keep organizer/admin access explicit and independent;
   - cover all inactive states with role-matrix regression tests.

2. **AS-03 tournament concurrency**
   - serialize invite-use and participant-capacity-sensitive writes;
   - preserve capacity/invite invariants under concurrent requests;
   - add deterministic concurrency tests.

3. **AS-05 public/private data boundary**
   - separate public DTOs from private/admin/internal data;
   - prevent account email and moderation/internal fields from leaking through public endpoints;
   - align privacy copy and migration behavior where required.

4. **AS-06 SSE connection pressure**
   - add per-source/user and global long-lived connection limits;
   - release limits correctly on disconnect/timeouts;
   - cover API/Nginx behavior and resource-pressure regressions.

5. **AS-02 privileged route access / MFA — operator-owned**
   - protect `/platform-ops*` and `/api/v1/admin*` with the approved Cloudflare/operator control;
   - retain application RBAC and audit events;
   - close only after direct dashboard/live evidence.

## P2 — hardening and cleanup

- AS-07: remove legacy R2 read contour and retained originals after approval.
- AS-08: negative cache/background refresh for unknown patch IDs.
- AS-09–AS-13: distributed auth counter, safe worker errors, config/CI drift and remaining audit hardening.
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
