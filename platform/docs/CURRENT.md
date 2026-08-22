# Platform current state

- Status: Active source of current production state
- Owner: Platform maintainers
- Last reviewed: 2026-08-22

Read this file for the current production baseline and next engineering priority. Use the documentation index for deeper task-specific context.

## Production baseline

- Public origin: `https://old-sparky.com` behind Cloudflare Full(strict) and Nginx Origin CA.
- Active stack: Next.js standalone, FastAPI/Gunicorn, Celery, PostgreSQL, Redis, Nginx and Cloudflare R2/CDN on one VPS.
- Platform database: `platformdb`, schema `platform`.
- Web, API and worker run under separate locked Unix identities with per-service runtime environments; the web process receives no backend database, session, R2, mail, Turnstile-secret or OpenAI credentials.
- API and worker share only the dedicated `oldsparky-media` staging group; worker state and web cache remain service-owned.
- Current deployed product includes secure Steam OpenID login/linking, mobile auth/profile/tournament polish, enforced nonce CSP, tournament lifecycle, deterministic Deadlock assignment, locked rosters, bracket progression, immutable releases and tested rollback.
- Frontend audit remediation for contract validation, permissions, auth/session states, async draft/search races, retry boundaries, internal navigation and i18n is resolved and deployed in release `frontend-audit-remediation-20260822T204107Z`; evidence is in [`archive/frontend-audit-remediation-2026-08-22.md`](archive/frontend-audit-remediation-2026-08-22.md).
- Cloudflare Access now protects `/platform-ops*` and `/api/v1/admin*` with an operator-scoped Allow policy and independent MFA; a fresh incognito login verified the identity -> TOTP MFA -> application path while application `admin`/`superadmin` RBAC remains authoritative.
- Cloudflare is the single visitor-facing HSTS owner. Dashboard verification on 2026-08-21 confirmed HSTS On with six-month `max-age=15552000`, `includeSubDomains` Off and preload Off; Nginx must continue to omit HSTS.
- Cloudflare Full(strict), minimum visitor TLS 1.2, TLS 1.3/HTTP3 and DNSSEC were operator-confirmed on 2026-08-21.
- Invite-only tournament workspace reads reject retained or otherwise inactive participant records; private bracket SSE also revalidates active participant membership while the connection remains open, so withdrawal/disqualification revokes an existing stream before further private events are emitted.
- Organizer participant removal is a retained `disqualified` record rather than a physical participant-row deletion. A disqualified participant cannot redeem another invite or self-rejoin that same tournament, and a retry does not consume another invite use. The organizer-only management roster retains inactive rows for explicit restoration; the exclusion remains scoped to that tournament and does not block participation in unrelated tournaments.
- Tournament invite claims/revocations and active participant-capacity mutations are transaction-serialized in PostgreSQL. Last invite use and last participant slot cannot be consumed twice, and restoring a retained inactive participant rechecks capacity before making the row active again.
- Anonymous public profile contracts omit account/contact email and Steam authentication identity. Public tournament participant/workspace contracts omit moderation note, moderator identity and moderation timestamps; organizer management uses a separate response DTO that retains those fields.
- Public tournament automation errors are persistence-sanitized before commit: `automation_last_error` can contain only the stable generic retry message, while restricted logs retain only tournament/failure metadata and a one-way error fingerprint. Migration `20260821_0039` rewrites historical non-null values to the same safe message.
- Public bracket SSE connection pressure is bounded in two layers: Redis-backed application leases enforce global, source and authenticated-user admission caps with fail-closed behavior, while Nginx adds coarse source/global connection caps. Rejections are observable and stream lifetime/reconnect behavior is bounded.
- Public media delivery is one-way `R2 -> CDN -> browser`: FastAPI exposes no `/api/v1/uploads/*` serving route, performs no render-path R2 object reads and has no R2-to-local-disk read fallback. Runtime serializers return only ready media-descriptor CDN URLs; historical `avatar_url`, `banner_url` and `cover_url` values are inert.
- Unknown public patch IDs return from the cache path without awaiting external content refresh. Per-ID negative caching and a Redis-coalesced global background-refresh gate bound miss amplification, while miss-triggered upstream requests refuse redirects and enforce a response-size limit.
- Password-login guessing protection uses independent source-IP and account-wide Redis state. Account identifiers are represented by HMAC fingerprints, shared failures drive adaptive Turnstile and a bounded cooldown, and successful login clears account failure/cooldown state.
- Alembic head: `20260822_0040`.
- Production contains the verified operator account and no test tournaments.

## Current engineering priority

**AS-16 — Test-suite audit and executable CI/live ownership** is resolved and
live-validated in production.
The audit remediation is tracked in [`test-suite-governance.md`](test-suite-governance.md):
deterministic backend/migration/web groups must run in CI, and production
browser QA must execute through the dedicated server wrapper.
The runtime release at `04d691b95b6ba9fde5982aed658523fe2e896407` passed the
security/build gate (`32589822458`), production deployment (`32590065060`) and
the full production browser gate (`32590276914`).

AS-15 — Deadlock persistence and workflow concurrency integrity is resolved and deployed. The release locks durable workflow/profile writers on
their stable parent rows, revalidates lifecycle state under lock, adds final
database guards and applies migration `20260822_0040`. Exact commit
`87525bab34c473ac51708eba1e242b7baa6a1462` is active as release
`gha-32574455599-1-87525bab34c4-20260822T125945Z`; closure evidence is in
[`archive/as-15-deadlock-workflow-integrity.md`](archive/as-15-deadlock-workflow-integrity.md).

No repository-owned P1 correctness remediation remains open. AS-12 is the
next operational hardening item and AS-13 remains separate CI revalidation
work.

## Production invariants

- Profile-level Deadlock dream slots are the source of truth.
- Invite-only workspace reads require active participant membership or explicit organizer/admin authority; historical inactive participant rows are not authorization grants, including for an already-open private bracket SSE stream.
- Organizer exclusion must retain the tournament participant row as `disqualified`; self-rejoin and same-tournament invite redemption remain blocked until the organizer deliberately restores an active status. This is tournament-scoped and must not become a platform-wide ban.
- Invite use and active-participant capacity are transaction-scoped PostgreSQL invariants: invite claim/revoke locks the stable tournament and invite rows in tournament-to-invite order; active-roster mutations serialize on the tournament row and capacity is rechecked before an inactive retained participant becomes active.
- Every Deadlock ready-check, captain, assignment generation, roster publish
  and roster-lock write path — API, automation and worker alike — locks its
  tournament row before checking lifecycle state. It re-reads terminal/staging
  status under that lock and locks secondary rows in one documented order.
  Redis may coalesce work but never replaces this durable transaction boundary.
- Ready-check votes must be committed only while their round is active and the
  voter remains an eligible active participant. A close or exclusion cannot
  leave a post-close or ineligible vote in persistence.
- The database is the final concurrency guard for cardinal workflow state:
  active ready-checks and the selected captain/assignment/roster state must not
  have ambiguous concurrent rows even if a future writer bypasses a service.
- Dream-slot replacement is serialized on the owning profile/user row. A
  replace-all request leaves exactly its selected profile-level slots, never a
  merge of concurrent payloads; slot values remain in the supported range.
- Public API contracts are explicit allowlists. Account/contact email and Steam authentication identity do not belong to anonymous public-profile DTOs, participant moderation metadata belongs only to organizer-management DTOs, and public automation error fields must never contain arbitrary exception text. A future public email feature requires a separate explicit opt-in contract rather than reusing account contact data.
- Public bracket SSE must retain layered application/Nginx connection caps, fail closed when Redis-backed admission state is unavailable, release leases on normal termination and retain bounded expiry recovery after abnormal termination. Stream lifetime and reconnect timing must remain bounded.
- Public media rendering must remain `R2 -> CDN -> browser`; normal API runtime must not proxy R2 objects, serve legacy upload paths or fall back to local-disk reads. Legacy URL columns and migration helpers may remain only while runtime-inert and migration/grace-period scoped.
- Unknown public patch IDs must not make the request path wait on external refresh work. Retain per-ID negative caching, cross-worker refresh coalescing and explicit no-redirect/response-size bounds for miss-triggered upstream requests.
- Password-login protection must retain independent per-IP and account-wide buckets. Account-wide Redis state must use private HMAC fingerprints rather than plaintext identifiers; cooldowns remain bounded and must not extend on blocked requests, and a successful login clears the account failure/cooldown state.
- Cloudflare Access is defense in depth only: privileged application RBAC remains authoritative after edge authentication/MFA succeeds.
- Cloudflare remains the single HSTS owner; do not add `Strict-Transport-Security` at Nginx while this ownership model is active.
- Terminal tournament states freeze organizer match administration.
- Preserve the live HTTPS/domain/secure-cookie contour unless a reviewed release changes it.
- Rollback switches application releases; it does not automatically downgrade Alembic.
- New production changes use immutable releases and pass the applicable preflight, smoke and focused live checks.

## Deferred / operator-owned work

- Remaining Cloudflare dashboard follow-up for CAA, WAF/rates and R2 settings where the operator checklist still marks work `VERIFY`/`TODO`.
- Real-user CSP follow-up and classification of new enforcement reports.
- Post-grace physical removal of runtime-inert legacy media URL columns/call-site plumbing and migration-only helpers when no longer required.
- Non-security feature expansion that does not remove a launch or production blocker.

For priorities and backlog, use [`platform-roadmap.md`](platform-roadmap.md). For evidence and details, follow the task router in [`README.md`](README.md).
