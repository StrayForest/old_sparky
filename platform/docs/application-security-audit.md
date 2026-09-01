# Application Security Audit — active findings

- Status: Active security/correctness tracker
- Owner: Platform maintainers
- Last reviewed: 2026-09-01
- Historical point-in-time audits and resolved findings: [`archive/`](archive/)

This document contains only findings that still require action or direct operator verification. Historical evidence is archived so routine security work does not load resolved context.

## Executive summary

| ID | Severity / priority | Confidence | Finding | Status |
|---|---|---:|---|---|
| AS-12 | Medium / P2 | High | Proxy, Cloudflare-range, UFW and startup validation can drift independently | Open |

## Authorization matrix

Application RBAC remains authoritative. Cloudflare Access is an additional exposure control and must never grant an application role.

| Surface | Anonymous | User | Active participant | Organizer | Admin | Superadmin |
|---|---|---|---|---|---|---|
| Public home, patches, public profiles and public tournament summary | Read | Read | Read | Read | Read | Read |
| Invite-only tournament summary | No, unless explicitly public metadata | With valid invite/access | Read | Read | Read | Read |
| Roster, bracket and matches | Public tournaments: read; invite-only: no | No unless actively joined | Read | Own tournament: read/manage | Read/manage | Read/manage |
| Own account/profile/media/session | No | Own records only | Own records only | Own records only | Own records only | Own records only |
| Join/leave, ready and captain workflow | No | Subject to workflow eligibility | Own participant actions | Participant actions plus own-tournament management | App rules plus admin operations | App rules plus admin operations |
| Tournament configuration, invites, moderation, bracket/results | No | No | No | Own tournament only | Administrative scope | Administrative scope |
| Admin console/API | No | No | No | No by organizer role alone | Yes | Yes |
| Role grants and destructive pre-production cleanup | No | No | No | No | No | Yes |

## Active findings

### AS-12 — Proxy and firewall configuration drift
Production settings and runners now reject non-loopback API/web binds and
non-loopback forwarded-header trust. Preflight can require a read-only parity
proof across current Cloudflare ranges, the Nginx include and managed UFW
rules. **Live proof remains open:** listener inspection, exact UFW/Nginx CIDR
comparison and a direct-origin negative test must be run on the VPS.

### Additional audit actions

- Canonical env group-read was removed; renderer/preflight now reject stale or
  unsafe service envs.
- The unit installer now installs the off-site-backup unit/timer and enables the
  reviewed maintenance, Cloudflare-range and health timers. Off-site backup
  activation remains behind its restore-drill gate.
- Post-deploy patch translation is reported as a controlled warm-up with an
  explicit OpenAI cache-miss call budget, not read-only QA.
- The production workflow builds and attests the immutable release and
  artifact-bound wheelhouse in CI, publishes its digest, and sends only that
  digest-verified artifact to the VPS. The VPS does not resolve dependencies or
  build from a source checkout.
- Normal production deployment is now automatically chained from a successful
  `Platform security and build` push run for the current `dev` HEAD. The
  `Platform production auto-deploy` gate rejects stale successful runs,
  requires the exact `platform-security-build=success` status and skips SHAs
  already marked `platform-production-deploy=success`; manual deploy remains an
  operator fallback rather than the routine path.
- The frontend audit follow-up hardens client authorization and asynchronous
  mutation state: anonymous invite-only page reads become an explicit login
  flow, private registration consumes the backend invite-access capability,
  auth/security feature fallbacks fail closed, Steam link UI requires confirmed
  runtime capability, tournament creation serializes submit/invite-code state,
  profile editors block overlapping saves, destructive pre-production cleanup
  no longer reports a committed cleanup as failed because a later reload failed,
  and invite-code readiness uses the same 10-character minimum as validation.
  Deterministic regression coverage in
  `apps/platform_web/tests/smoke/frontend-audit-regressions.spec.ts` locks these
  contracts.

## Remediation order

1. Complete AS-12 live proof.

AS-13 was closed on 2026-09-01 after the exact-SHA security/build run passed;
the evidence is retained in
[`archive/application-security-as-13-2026-09-01.md`](archive/application-security-as-13-2026-09-01.md).


Any newly confirmed Critical issue or direct authentication bypass blocks production installation. Do not widen CSP, disable Turnstile, add privileged-route bypasses or weaken application RBAC to simplify testing.
