# Application Security Audit — active findings

- Status: Active security/correctness tracker
- Owner: Platform maintainers
- Last reviewed: 2026-11-14
- Historical point-in-time audits and resolved findings: [`archive/`](archive/)

This document contains only findings that still require action or direct operator verification. Historical evidence is archived so routine security work does not load resolved context.

## Executive summary

| ID | Severity / priority | Confidence | Finding | Status |
|---|---|---:|---|---|
| AUD-02 | P2 | High | Cloudflare dashboard/runtime evidence reconciliation | Open — cache rule/runtime boundary closed; operator controls remain |

## Authorization matrix

Application RBAC remains authoritative. Cloudflare Access is an additional exposure control and must never grant an application role.

| Surface | Anonymous | User | Active participant | Organizer | Admin | Superadmin |
|---|---|---|---|---|---|---|
| Public home, patches, public profiles and public tournament summary | Read | Read | Read | Read | Read | Read |
| Invite-only tournament summary | Valid nonrevoked/nonexpired bearer code | Authenticated or valid bearer code | Read | Read | Read | Read |
| Roster, bracket and matches | Public tournaments: read; invite-only: valid bearer code | Active membership or valid bearer code | Read | Own tournament: read/manage | Read/manage | Read/manage |
| Own account/profile/media/session | No | Own records only | Own records only | Own records only | Own records only | Own records only |
| Join/leave, ready and captain workflow | No | Subject to workflow eligibility | Own participant actions | Participant actions plus own-tournament management | App rules plus admin operations | App rules plus admin operations |
| Tournament configuration, invites, moderation, bracket/results | No | No | No | Own tournament only | Administrative scope | Administrative scope |
| Admin console/API | No | No | No | No by organizer role alone | Yes | Yes |
| Role grants and destructive pre-production cleanup | No | No | No | No | No | Yes |

### Private invite bearer boundary

The accepted bearer-read contract is intentionally narrower than the
historical broad `/{slug}/...` child guard. A valid nonrevoked, nonexpired
invite code is accepted only on the exact `GET` routes for summary, workspace,
participants, matches and bracket. The matched route template and parsed slug
are checked against a closed allowlist; new child routes do not inherit bearer
access automatically. Profile, invite-management and Deadlock workflow reads,
all mutation-bearing `POST`/`PATCH`/`DELETE` routes, and every
membership/workflow mutation stay outside the capability. The legacy
`POST /api/v1/tournaments/invites/claim` remains a separate read-only
compatibility envelope for body-code lookup; it performs no access, membership
or usage-counter write and does not grant the bearer capability. Retained
inactive participants are allowed on the five read routes only when they
present a valid code.

The query parser accepts one 10–24-character ASCII alphanumeric code and
fails closed for duplicate/malformed, expired, revoked or wrong-tournament
values. Closed/completed tournaments remain readable with a valid code. Direct
bearer attempts use an HMAC-derived IP/code Redis bucket; Redis failure is a
typed fail-closed `503`, while cookie-only reads without a code do not depend
on that limiter. Workspace serialization omits a default invite and can echo
only the exact validated presented code. Invite usage counters remain
compatibility metadata and are neither read authorization nor consumption
state. Tournament creation and invite-code status lookup call the same strict
raw-code validator before any resource/invite persistence; malformed
punctuation, Unicode, control and whitespace values receive a generic `422`
without the credential in the error body. Bracket conditional requests perform
the current authn/authz, exact invite and rate-limit checks before ETag
comparison; the ETag covers only authorization-independent bracket state, and
denied requests receive neither a `304` nor private representation headers.
The decision and compatibility impact are recorded in the
[private tournament bearer-read ADR](adr/private-tournament-bearer-read-boundary.md).

## Active findings

### AUD-02 — Cloudflare dashboard/runtime evidence reconciliation

The public catalog cache behavior is now live-proven and the previous
documentation contradiction is closed: warmed anonymous requests produce
`HIT`, while the actual production session cookie and `Authorization` produce
`DYNAMIC`; `/api/v1/tournaments/mine` remains private and uncached. Evidence
is in [`archive/as-02-cloudflare-catalog-cache-2026-09-05.md`](archive/as-02-cloudflare-catalog-cache-2026-09-05.md).

The finding is narrowed but remains open for media-token scope, provider-plan
availability of Universal SSL lifecycle alerts and Managed WAF entitlement.
The CAA policy, Certificate Transparency alerting, one plan-compatible login
edge rate limit, deliberate Bot Fight Mode-disabled decision and daily
Cloudflare-range/UFW alert workflow are now evidenced. Details are in
[`archive/as-02-cloudflare-operator-remediation-2026-09-05.md`](archive/as-02-cloudflare-operator-remediation-2026-09-05.md).
The checklist must not be called fully closed until the remaining
secret-scope and provider-entitlement checks have evidence.

## Remediation order

Complete the remaining AUD-02 dashboard/operator checks in
[`cloudflare-production-checklist.md`](cloudflare-production-checklist.md).


Any newly confirmed Critical issue or direct authentication bypass blocks production installation. Do not widen CSP, disable Turnstile, add privileged-route bypasses or weaken application RBAC to simplify testing.
