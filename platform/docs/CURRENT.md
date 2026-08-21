# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-08-16

Read this file for the current production baseline and next engineering priority. Use the documentation index for deeper task-specific context.

## Production baseline

- Public origin: `https://old-sparky.com` behind Cloudflare Full(strict) and Nginx Origin CA.
- Active stack: Next.js standalone, FastAPI/Gunicorn, Celery, PostgreSQL, Redis, Nginx and Cloudflare R2/CDN on one VPS.
- Platform database: `platformdb`, schema `platform`.
- Current deployed product includes secure Steam OpenID login/linking, mobile auth/profile/tournament polish, enforced nonce CSP, tournament lifecycle, deterministic Deadlock assignment, locked rosters, bracket progression, immutable releases and tested rollback.
- Alembic head: `20260813_0038`.
- Production contains the verified operator account and no test tournaments.

## Current engineering priority

Resolve application-security finding **AS-01: runtime secret isolation** before ordinary feature expansion.

Required result:

1. Separate Unix identities for web, API and worker.
2. Per-service environment/credential inputs; frontend receives no backend secrets.
3. Narrow ownership of upload staging, worker scratch space and process visibility.
4. Fail-closed validation for forbidden environment variables and unsafe bind/proxy configuration.
5. Release/install/rollback/health coverage without changing tournament behavior or DB schema unnecessarily.

After AS-01, the next narrow security package is AS-03/AS-04: invite/capacity serialization and inactive-participant authorization.

## Production invariants

- Profile-level Deadlock dream slots are the source of truth.
- Invite-only reads remain scoped to authorized users.
- Terminal tournament states freeze organizer match administration.
- Preserve the live HTTPS/domain/secure-cookie contour unless a reviewed release changes it.
- Rollback switches application releases; it does not automatically downgrade Alembic.
- New production changes use immutable releases and pass the applicable preflight, smoke and focused live checks.

## Deferred / operator-owned work

- Cloudflare dashboard evidence for HSTS, DNSSEC, CAA, WAF/rates, edge TLS and R2 settings.
- Real-user CSP follow-up and classification of new enforcement reports.
- Non-security feature expansion that does not remove a launch or production blocker.

For priorities and backlog, use [`platform-roadmap.md`](platform-roadmap.md). For evidence and details, follow the task router in [`README.md`](README.md).
