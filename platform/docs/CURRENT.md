# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-08-21

Read this file for the current production baseline and next engineering priority. Use the documentation index for deeper task-specific context.

## Production baseline

- Public origin: `https://old-sparky.com` behind Cloudflare Full(strict) and Nginx Origin CA.
- Active stack: Next.js standalone, FastAPI/Gunicorn, Celery, PostgreSQL, Redis, Nginx and Cloudflare R2/CDN on one VPS.
- Platform database: `platformdb`, schema `platform`.
- Web, API and worker run under separate locked Unix identities with per-service runtime environments; the web process receives no backend database, session, R2, mail, Turnstile-secret or OpenAI credentials.
- API and worker share only the dedicated `oldsparky-media` staging group; worker state and web cache remain service-owned.
- Current deployed product includes secure Steam OpenID login/linking, mobile auth/profile/tournament polish, enforced nonce CSP, tournament lifecycle, deterministic Deadlock assignment, locked rosters, bracket progression, immutable releases and tested rollback.
- Invite-only tournament workspace reads now reject retained `withdrawn`/`disqualified` participant records; access requires active participation or explicit organizer/admin authority.
- Alembic head: `20260813_0038`.
- Production contains the verified operator account and no test tournaments.

## Current engineering priority

Resolve the remaining application-security/correctness P1 work in this order:

1. **AS-03 invite/capacity serialization** — make invite use and participant-capacity check-and-write operations atomic under concurrency.
2. **AS-05 public/private data boundary** — prevent account contact and moderation/internal fields from crossing public API contracts.
3. **AS-06 SSE connection pressure** — add bounded per-source/global long-lived connection controls and regression coverage.

AS-02 remains an operator-owned Cloudflare Access/MFA verification task for `/platform-ops*` and `/api/v1/admin*` and does not replace application RBAC.

## Production invariants

- Profile-level Deadlock dream slots are the source of truth.
- Invite-only workspace reads require active participant membership or explicit organizer/admin authority; historical inactive participant rows are not authorization grants.
- Terminal tournament states freeze organizer match administration.
- Preserve the live HTTPS/domain/secure-cookie contour unless a reviewed release changes it.
- Rollback switches application releases; it does not automatically downgrade Alembic.
- New production changes use immutable releases and pass the applicable preflight, smoke and focused live checks.

## Deferred / operator-owned work

- Cloudflare dashboard evidence for HSTS, DNSSEC, CAA, WAF/rates, edge TLS and R2 settings.
- Real-user CSP follow-up and classification of new enforcement reports.
- Non-security feature expansion that does not remove a launch or production blocker.

For priorities and backlog, use [`platform-roadmap.md`](platform-roadmap.md). For evidence and details, follow the task router in [`README.md`](README.md).
