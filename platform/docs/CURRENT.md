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
- Invite-only tournament workspace reads reject retained or otherwise inactive participant records; private bracket SSE also revalidates active participant membership while the connection remains open, so withdrawal/disqualification revokes an existing stream before further private events are emitted.
- Organizer participant removal is a retained `disqualified` record rather than a physical participant-row deletion. A disqualified participant cannot redeem another invite or self-rejoin that same tournament, and a retry does not consume another invite use. The organizer-only management roster retains inactive rows for explicit restoration; the exclusion remains scoped to that tournament and does not block participation in unrelated tournaments.
- Tournament invite claims/revocations and active participant-capacity mutations are transaction-serialized in PostgreSQL. Last invite use and last participant slot cannot be consumed twice, and restoring a retained inactive participant rechecks capacity before making the row active again.
- Anonymous public profile contracts omit account/contact email and Steam authentication identity. Public tournament participant/workspace contracts omit moderation note, moderator identity and moderation timestamps; organizer management uses a separate response DTO that retains those fields.
- Public bracket SSE connection pressure is bounded in two layers: Redis-backed application leases enforce global, source and authenticated-user admission caps with fail-closed behavior, while Nginx adds coarse source/global connection caps. Rejections are observable and stream lifetime/reconnect behavior is bounded.
- Public media delivery is one-way `R2 -> CDN -> browser`: FastAPI exposes no `/api/v1/uploads/*` serving route, performs no render-path R2 object reads and has no R2-to-local-disk read fallback. Runtime serializers return only ready media-descriptor CDN URLs; historical `avatar_url`, `banner_url` and `cover_url` values are inert.
- Unknown public patch IDs return from the cache path without awaiting external content refresh. Per-ID negative caching and a Redis-coalesced global background-refresh gate bound miss amplification, while miss-triggered upstream requests refuse redirects and enforce a response-size limit.
- Password-login guessing protection uses independent source-IP and account-wide Redis state. Account identifiers are represented by HMAC fingerprints, shared failures drive adaptive Turnstile and a bounded cooldown, and successful login clears account failure/cooldown state.
- Alembic head: `20260813_0038`.
- Production contains the verified operator account and no test tournaments.

## Current engineering priority

No repository-owned P1 implementation remains after AS-06 closure.

AS-02 remains an operator-owned Cloudflare Access/MFA verification task for `/platform-ops*` and `/api/v1/admin*` and does not replace application RBAC. AS-07 runtime media cleanup, AS-08 unknown-patch refresh hardening and AS-09 distributed login guessing protection are closed and archived. AS-10 remains a product/security decision on registration-enumeration behavior; the next bounded code-owned remediation target is AS-11 public worker-error sanitization.

## Production invariants

- Profile-level Deadlock dream slots are the source of truth.
- Invite-only workspace reads require active participant membership or explicit organizer/admin authority; historical inactive participant rows are not authorization grants, including for an already-open private bracket SSE stream.
- Organizer exclusion must retain the tournament participant row as `disqualified`; self-rejoin and same-tournament invite redemption remain blocked until the organizer deliberately restores an active status. This is tournament-scoped and must not become a platform-wide ban.
- Invite use and active-participant capacity are transaction-scoped PostgreSQL invariants: invite claim/revoke locks the stable tournament and invite rows in tournament-to-invite order; active-roster mutations serialize on the tournament row and capacity is rechecked before an inactive retained participant becomes active.
- Public API contracts are explicit allowlists. Account/contact email and Steam authentication identity do not belong to anonymous public-profile DTOs, and participant moderation metadata belongs only to organizer-management DTOs. A future public email feature requires a separate explicit opt-in contract rather than reusing account contact data.
- Public bracket SSE must retain layered application/Nginx connection caps, fail closed when Redis-backed admission state is unavailable, release leases on normal termination and retain bounded expiry recovery after abnormal termination. Stream lifetime and reconnect timing must remain bounded.
- Public media rendering must remain `R2 -> CDN -> browser`; normal API runtime must not proxy R2 objects, serve legacy upload paths or fall back to local-disk reads. Legacy URL columns and migration helpers may remain only while runtime-inert and migration/grace-period scoped.
- Unknown public patch IDs must not make the request path wait on external refresh work. Retain per-ID negative caching, cross-worker refresh coalescing and explicit no-redirect/response-size bounds for miss-triggered upstream requests.
- Password-login protection must retain independent per-IP and account-wide buckets. Account-wide Redis state must use private HMAC fingerprints rather than plaintext identifiers; cooldowns remain bounded and must not extend on blocked requests, and a successful login clears the account failure/cooldown state.
- Terminal tournament states freeze organizer match administration.
- Preserve the live HTTPS/domain/secure-cookie contour unless a reviewed release changes it.
- Rollback switches application releases; it does not automatically downgrade Alembic.
- New production changes use immutable releases and pass the applicable preflight, smoke and focused live checks.

## Deferred / operator-owned work

- Cloudflare dashboard evidence for HSTS, DNSSEC, CAA, WAF/rates, edge TLS and R2 settings.
- Real-user CSP follow-up and classification of new enforcement reports.
- Post-grace physical removal of runtime-inert legacy media URL columns/call-site plumbing and migration-only helpers when no longer required.
- Non-security feature expansion that does not remove a launch or production blocker.

For priorities and backlog, use [`platform-roadmap.md`](platform-roadmap.md). For evidence and details, follow the task router in [`README.md`](README.md).