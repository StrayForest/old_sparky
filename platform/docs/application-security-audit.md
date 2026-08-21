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
| AS-03 | Medium / P1 | High | Invite-use and participant-capacity checks are not serialised | Open |
| AS-04 | Medium / P1 | High | Inactive participants can satisfy private-workspace access checks | Open; next implementation target |
| AS-05 | Medium / P1 | High | Contact and moderation fields cross the documented public-data boundary | Open |
| AS-06 | Medium / P1 | High | Public SSE has no bounded per-source/global connection cap | Open |
| AS-07 | Medium / P2 | High | Legacy upload/R2 read paths retain originals and buffer entire objects | Open; destructive removal requires approval |
| AS-08 | Medium / P2 | High | Unknown public patch IDs can trigger synchronous external refresh work | Open |
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
| Roster, bracket, matches and SSE | Public tournaments: read; invite-only: no | No unless joined | Read | Own tournament: read/manage | Read/manage | Read/manage |
| Own account/profile/media/session | No | Own records only | Own records only | Own records only | Own records only | Own records only |
| Join/leave, ready and captain workflow | No | Subject to workflow eligibility | Own participant actions | Participant actions plus own-tournament management | App rules plus admin operations | App rules plus admin operations |
| Tournament configuration, invites, moderation, bracket/results | No | No | No | Own tournament only | Administrative scope | Administrative scope |
| Admin console/API | No | No | No | No by organizer role alone | Yes | Yes |
| Role grants and destructive pre-production cleanup | No | No | No | No | No | Yes |

The inactive-participant exception to the intended matrix is AS-04.

## P1 findings

### AS-04 — Inactive participant records retain workspace visibility

- Risk: a user who once joined an invite-only tournament can retain access after withdrawal, rejection, disqualification or another inactive transition.
- Evidence owner: tournament workspace authorization helpers in `apps/platform_api/app/api/routes/tournaments.py`.
- Required result: every private-workspace gate uses active/joined participation, while organizer/admin authorization stays explicit and independent.
- Verification: role-matrix tests for every inactive status across roster, bracket, match and SSE reads.
- Status: **Open; next implementation target**.

### AS-03 — Invite and capacity decisions race

- Risk: concurrent requests can consume the last invite use or participant slot more than once.
- Evidence owner: invite claim, organizer participant-add and self-join check-and-write paths in `apps/platform_api/app/api/routes/tournaments.py`.
- Required result: lock a stable tournament/invite row before check-and-write; add database-enforced invariants where practical.
- Verification: deterministic concurrent integration tests for last invite use and last participant slot.
- Status: **Open**.

### AS-05 — Public data exceeds the privacy contract

- Risk: account contact email or internal moderation context can cross public API/profile boundaries.
- Evidence owner: registration/profile serialization and public participant DTOs.
- Required result: explicit public DTOs with opt-in contact data and no moderation/internal fields; align existing data/privacy behavior.
- Verification: anonymous API tests proving private/internal fields never appear.
- Status: **Open**.

### AS-06 — Unbounded public SSE connections

- Risk: many long-lived public SSE connections can exhaust file descriptors, API/Redis connections or worker capacity.
- Evidence owner: public tournament SSE route, bracket event service and Nginx SSE location.
- Required result: bounded per-source/user and global connection pressure, clean disconnect/timeout release, sensible reconnect behavior and observable rejection.
- Verification: concurrency/resource-limit tests plus Nginx/API smoke coverage.
- Status: **Open**.

### AS-02 — Privileged route edge protection not proven

- Risk: `/platform-ops*` and `/api/v1/admin*` may lack the intended Cloudflare Access/MFA exposure boundary, although application RBAC remains authoritative.
- Required result: direct Cloudflare configuration/live evidence for the exact privileged routes, MFA policy and break-glass behavior.
- Constraint: repository changes alone cannot prove closure.
- Status: **Open; operator/dashboard evidence required**.

## P2 findings

### AS-07 — Legacy upload and R2 read contour
Remove retained originals and the shadowed legacy read path only after approved inventory/backup verification; avoid whole-object API buffering. **Open; destructive remediation requires approval.**

### AS-08 — Patch cache misses trigger external refreshes
Unknown public patch IDs must use negative caching/coalesced background refresh instead of arbitrary synchronous external work; validate redirect/response bounds. **Open.**

### AS-09 — Distributed login guessing residual
Add a privacy-preserving account-wide failure window/cooldown without creating a permanent lockout DoS path. **Open.**

### AS-10 — Existing-email enumeration at registration
Decide whether duplicate-registration UX justifies disclosure; otherwise use a generic accepted flow with comparable timing/Turnstile behavior. **Open; product/security decision required.**

### AS-11 — Worker exception text in public responses
Expose stable public error codes/generic text and retain redacted diagnostics only in restricted logs/admin surfaces. **Open.**

### AS-12 — Proxy and firewall configuration drift
Fail closed on production bind/proxy invariants and keep Nginx/Cloudflare/UFW trust ranges reconciled. **Open.**

### AS-13 — CI isolation contour needs current revalidation
The current workflow has been restored and is green, but the older finding must be rechecked against the exact supported fail-closed test runner/config before closure or modification. **Open pending revalidation.**

## Operational finding

### AS-14 — HSTS ownership/state requires direct verification
Verify the actual Cloudflare HSTS owner/value and rollback implications. Nginx must not independently add or alter HSTS without that evidence. **Open; operator verification required.**

## Remediation order

1. AS-04 inactive-participant authorization.
2. AS-03 invite/capacity serialization.
3. AS-05 public/private data boundary.
4. AS-06 SSE connection pressure.
5. AS-02 Cloudflare Access/MFA verification.
6. AS-07 through AS-13 as bounded P2 packages; separate destructive, network-policy and product/privacy decisions where required.
7. AS-14 remains operator verification work.

Any newly confirmed Critical issue or direct authentication bypass blocks production installation. Do not widen CSP, disable Turnstile, add privileged-route bypasses or weaken application RBAC to simplify testing.
