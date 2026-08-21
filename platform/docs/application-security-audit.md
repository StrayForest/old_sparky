# Application Security Audit — active findings

- Status: Active security/correctness tracker
- Owner: Platform maintainers
- Last reviewed: 2026-08-21
- Historical point-in-time audits and resolved findings: [`archive/`](archive/)

This document contains only findings that still require action or direct operator verification. Historical evidence is archived so routine security work does not load resolved context.

## Executive summary

| ID | Severity / priority | Confidence | Finding | Status |
|---|---|---:|---|---|
| AS-02 | High / P1 | High | Cloudflare Access/MFA for privileged routes is not directly verified | Open; operator/dashboard evidence required |
| AS-09 | Medium / P2 | Medium | Login protection has no account-wide counter across source IPs | Open |
| AS-10 | Low / P2 | High | Registration can reveal an existing email address | Open; product/security decision required |
| AS-11 | Low / P2 | High | Public tournament responses can expose worker exception text | Open |
| AS-12 | Medium / P2 | High | Proxy, Cloudflare-range, UFW and startup validation can drift independently | Open |
| AS-13 | Low / P2 | Medium | CI fail-closed isolation contour requires revalidation against the current workflow | Open; revalidate before changing |
| AS-14 | Operational / P1 | High | HSTS ownership/state requires direct Cloudflare verification | Open; operator verification required |

## Authorization matrix

Application RBAC remains authoritative. Cloudflare Access is an additional exposure control and must never grant an application role.

| Surface | Anonymous | User | Active participant | Organizer | Admin | Superadmin |
|---|---|---|---|---|---|---|
| Public home, patches, public profiles and public tournament summary | Read | Read | Read | Read | Read | Read |
| Invite-only tournament summary | No, unless explicitly public metadata | With valid invite/access | Read | Read | Read | Read |
| Roster, bracket, matches and SSE | Public tournaments: read; invite-only: no | No unless actively joined | Read | Own tournament: read/manage | Read/manage | Read/manage |
| Own account/profile/media/session | No | Own records only | Own records only | Own records only | Own records only | Own records only |
| Join/leave, ready and captain workflow | No | Subject to workflow eligibility | Own participant actions | Participant actions plus own-tournament management | App rules plus admin operations | App rules plus admin operations |
| Tournament configuration, invites, moderation, bracket/results | No | No | No | Own tournament only | Administrative scope | Administrative scope |
| Admin console/API | No | No | No | No by organizer role alone | Yes | Yes |
| Role grants and destructive pre-production cleanup | No | No | No | No | No | Yes |

## P1 findings

### AS-02 — Privileged route edge protection not proven

- Risk: `/platform-ops*` and `/api/v1/admin*` may lack the intended Cloudflare Access/MFA exposure boundary, although application RBAC remains authoritative.
- Required result: direct Cloudflare configuration/live evidence for the exact privileged routes, MFA policy and break-glass behavior.
- Constraint: repository changes alone cannot prove closure.
- Status: **Open; operator/dashboard evidence required**.

## P2 findings

### AS-09 — Distributed login guessing residual
Add a privacy-preserving account-wide failure window/cooldown without creating a permanent lockout DoS path. **Open.**

### AS-10 — Existing-email enumeration at registration
Decide whether duplicate-registration UX justifies disclosure; otherwise use a generic accepted flow with comparable timing/Turnstile behavior. **Open; product/security decision required**.

### AS-11 — Worker exception text in public responses
Expose stable public error codes/generic text and retain redacted diagnostics only in restricted logs/admin surfaces. **Open.**

### AS-12 — Proxy and firewall configuration drift
Fail closed on production bind/proxy invariants and keep Nginx/Cloudflare/UFW trust ranges reconciled. **Open.**

### AS-13 — CI isolation contour needs current revalidation
The current workflow has been restored and is green, but the older finding must be rechecked against the exact supported fail-closed test runner/config before closure or modification. **Open pending revalidation.**

## Operational finding

### AS-14 — HSTS ownership/state requires direct verification
Verify the actual Cloudflare HSTS owner/value and rollback implications. Nginx must not independently add or alter HSTS without that evidence. **Open; operator verification required**.

## Remediation order

1. AS-02 Cloudflare Access/MFA verification remains the only open P1 security finding that is directly owner-gated outside the repository.
2. AS-09 through AS-13 proceed as bounded P2 packages; separate network-policy and product/privacy decisions where required.
3. AS-14 remains operator verification work.

Resolved AS-03 evidence is retained in [`archive/as-03-tournament-write-serialization.md`](archive/as-03-tournament-write-serialization.md). Resolved AS-05 evidence is retained in [`archive/as-05-public-private-data-boundary.md`](archive/as-05-public-private-data-boundary.md). Resolved AS-06 evidence is retained in [`archive/as-06-sse-connection-pressure.md`](archive/as-06-sse-connection-pressure.md). Resolved AS-07 evidence is retained in [`archive/as-07-r2-cdn-runtime-cleanup.md`](archive/as-07-r2-cdn-runtime-cleanup.md). Resolved AS-08 evidence is retained in [`archive/as-08-patch-miss-hardening.md`](archive/as-08-patch-miss-hardening.md).

Any newly confirmed Critical issue or direct authentication bypass blocks production installation. Do not widen CSP, disable Turnstile, add privileged-route bypasses or weaken application RBAC to simplify testing.