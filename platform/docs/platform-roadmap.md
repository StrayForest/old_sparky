# Platform roadmap

- Status: Active backlog and priority map
- Owner: Platform maintainers
- Last reviewed: 2026-08-16

For the production baseline and immediate engineering target, read [`CURRENT.md`](CURRENT.md). This file deliberately omits release diaries and detailed audit evidence.

## P1 — security and correctness

1. **AS-01 runtime identities and per-service secrets**
   - separate web/API/worker Unix identities;
   - per-service credentials/environment;
   - least-privilege staging/scratch/process visibility;
   - fail-closed runtime validation;
   - install/restart/rollback coverage.

2. **AS-02 privileged route access / MFA**
   - protect `/platform-ops*` and `/api/v1/admin*` with the approved Cloudflare/operator control;
   - retain application RBAC and audit events.

3. **AS-03/AS-04 tournament concurrency and inactive-participant authorization**
   - serialize invite/capacity-sensitive writes;
   - ensure withdrawn/disqualified/inactive participants cannot retain workflow access they no longer own.

4. **AS-05/AS-06 public-data/privacy contract and SSE caps**
   - make public/private data exposure explicit;
   - bound long-lived connection pressure at API/Nginx boundaries.

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

Detailed findings, evidence and CWE/OWASP mapping belong in [`application-security-audit.md`](application-security-audit.md). CSP policy/operations belong in [`security-runbook.md`](security-runbook.md). Release commands and rollback procedure belong in [`deployment-runbook.md`](deployment-runbook.md).
