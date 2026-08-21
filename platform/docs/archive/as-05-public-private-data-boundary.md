# AS-05 public/private data boundary — closed

- Status: Archived / resolved
- Closed: 2026-08-21
- Public DTO implementation commit: `0cfb732adf049b62071d7774ebb1a66abf360a31`
- Contract regression follow-up commit: `f18dfd18679f4debb6ca440e1e29d8bb6aa1d7a1`
- Privacy-copy alignment commit: `43f2dcf48dc94d60b5a6e5ff505b69113bf3e730`
- Security/build verification run: `32470064442`
- Production deployment run: `32470310856`
- Verified production release: `gha-32470310856-1-43f2dcf48dc9-20260821T095754Z`
- Alembic head: `20260813_0038` (unchanged)

## Original finding

The legacy public API boundary mixed public and private concerns in response contracts:

- `PublicProfileResponse` advertised `contact_email`, `steam_id` and `steam_linked`, even where runtime code attempted to suppress values;
- public tournament participant/workspace responses reused the participant response shape that also carried `moderation_note`, `moderated_at` and `moderated_by_user_id`;
- the privacy page allowed a broader interpretation of public Steam identity than the safer API contract selected during remediation.

A nullable sensitive field was not treated as a sufficient privacy boundary. Existing profile contact data may be populated from owner/account flows, so retaining that field in an anonymous public DTO would leave the product one serialization regression away from disclosure.

## Audit scope

The remediation review covered:

- `PublicProfileResponse` and `GET /profiles/public/{handle}`;
- owner/private profile responses and the account-contact lifecycle;
- public tournament participant and workspace serialization;
- organizer-only `GET /tournaments/{slug}/participants/manage`;
- OpenAPI response schemas and existing profile/participant regression tests;
- the public privacy policy language for email and Steam identity.

Adjacent findings were deliberately kept separate: tournament-scoped authenticated profile access was not converted into an anonymous public surface, worker exception exposure remains AS-11, and SSE connection pressure remains AS-06.

## Decision

Public response models are explicit allowlists rather than private models with sensitive keys set to null.

- Anonymous public profiles do not contain account/contact email, SteamID or Steam-link state.
- Existing stored contact data remains available only through the intended owner/private flows; no destructive data migration was required.
- Discord and region remain user-selected public profile fields.
- A future public email feature must introduce a separate explicit opt-in field/contract and consent UX rather than reuse account/contact email.
- Public participant/workspace payloads do not contain moderation note, moderator identity or moderation timestamps.
- Organizer participant management uses the dedicated `TournamentParticipantManagementResponse` and retains moderation metadata there.

## Remediation delivered

Commit `0cfb732adf049b62071d7774ebb1a66abf360a31` introduced the split public/management DTO boundary and dedicated AS-05 integration/OpenAPI coverage.

Commit `f18dfd18679f4debb6ca440e1e29d8bb6aa1d7a1` updated the existing profile regression suite to require sensitive public keys to be absent rather than present with null/false values.

Commit `43f2dcf48dc94d60b5a6e5ff505b69113bf3e730` aligned the public privacy policy with the implemented boundary: email, confirmed SteamID and Steam-link state are not public.

## Verification

Security/build run `32470064442` passed the full supported contour for commit `43f2dcf48dc94d60b5a6e5ff505b69113bf3e730`:

- backend suite: `649` tests, `1` skipped;
- anonymous public profile regression with populated private contact data proving `account_email`, `contact_email`, `steam_id` and `steam_linked` are absent;
- anonymous public roster regression with a real moderation note proving `moderation_note`, `moderated_at` and `moderated_by_user_id` are absent while organizer management still receives them;
- OpenAPI contract regression proving the same public/private separation structurally;
- `pip-audit`, Ruff, Bandit and tracked-file secret scanning;
- frontend npm audit, typecheck, lint and production build;
- Playwright web smoke.

Production deployment run `32470310856` checked out exact commit `43f2dcf48dc94d60b5a6e5ff505b69113bf3e730`, built and checksum-verified immutable release `gha-32470310856-1-43f2dcf48dc9-20260821T095754Z`, installed it successfully and kept Alembic at `20260813_0038`. API, worker, web and Nginx were active after restart; origin and public deployment smoke, CSP checks and SSE smoke passed.

No Cloudflare, Turnstile, CSP, application RBAC or authentication control was weakened.

## Remaining scope

AS-06 is the next repository-owned P1 implementation target and owns bounded SSE connection pressure, disconnect/timeout release and regression coverage. AS-02 remains operator-owned Cloudflare Access/MFA verification. AS-11 separately owns public worker exception text.
