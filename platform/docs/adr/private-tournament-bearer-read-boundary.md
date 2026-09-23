# Private tournament bearer read boundary

- Status: Accepted
- Date: 2026-09-13
- Owner: Platform API and security
- Supersedes: the broad future-child route rule recorded in
  [`archive/as-04-inactive-private-workspace-access.md`](../archive/as-04-inactive-private-workspace-access.md)

## Context

Invite-only tournament reads need a shareable, anonymous capability for a
private tournament page. A bearer code must not create a user, participant,
membership, claim, or workflow state. The earlier AS-04 guard treated every
future `GET /tournaments/{slug}/...` child as part of the private-read
boundary. That made an accidentally added child route eligible for the same
bearer bypass as workspace data and made the route contract difficult to
review.

Invite records also retain historical `max_uses` and `use_count` columns.
Invite reads are intentionally unlimited and non-consuming, so those legacy
counters cannot be an authorization authority. A workspace response must not
discover and return a different invite merely because it is the first
unrevoked row.

## Decision

1. A syntactically valid, nonrevoked, nonexpired invite code grants only
   read-only access to these API surfaces:

   - `GET /api/v1/tournaments/{slug}` (summary);
   - `GET /api/v1/tournaments/{slug}/workspace`;
   - `GET /api/v1/tournaments/{slug}/participants`;
   - `GET /api/v1/tournaments/{slug}/matches`;
   - `GET /api/v1/tournaments/{slug}/bracket`.

   The matched FastAPI route template is checked against a closed allowlist,
   and the slug is taken only from the matched route's parsed `slug` value.
   Every new child route is denied bearer access until it is explicitly opted
   in and covered by the route and API matrix tests.

2. Bearer access does not grant `POST`, `PATCH`, or `DELETE`, invite
   management, profile reads, Ready Check/captain/assignment workflow reads,
   or any workflow/membership mutation. An inactive retained participant may
   use the five allowlisted reads with a valid code, but remains denied on all
   other tournament surfaces. Existing organizer, admin and active-member
   authorization remains independent and authoritative.

3. The bearer query parser accepts exactly one `invite_code` value containing
   10–24 ASCII letters or digits (case-insensitive, canonicalized to
   uppercase). Duplicate or malformed values return `422`; syntactically valid
   expired, revoked, or wrong-tournament values fail closed through the normal
   visibility boundary (`401` for an anonymous private read and `403` for an
   inactive authenticated viewer). Closed and completed tournaments remain
   readable with a valid code; workflow and join rules still govern writes.

   The same strict raw-code validator is used for custom tournament creation,
   invite-code status lookup and generated invite creation. It never strips,
   filters, transliterates or otherwise repairs punctuation, Unicode, control,
   whitespace or out-of-range input. Invalid creation/status input returns a
   generic `422` without echoing the credential and before a tournament or
   invite can be persisted; lower-case valid input is stored in uppercase.

4. Workspace serialization omits invite codes unless the request supplied a
   valid code. If it echoes one, it echoes only the exact validated invite row
   for that request. It never selects a first/default unrevoked invite.

5. Direct bearer attempts are rate-limited in Redis by an HMAC-derived
   source-IP/code pair. The raw code is never used as a key or written to
   logs. The default budget is 60 attempts per 15-minute bucket for each pair;
   generated codes contain 10 random ASCII alphanumeric characters, while
   expiry and explicit revocation remain the durable invalidation controls.
   Redis failure returns the existing typed fail-closed `503` for bearer
   attempts; cookie-only internal reads without a bearer code do not enter this
   limiter. No read path increments or consumes invite usage.

   Bracket GETs complete the current authentication, authorization, exact
   invite and rate-limit checks before evaluating `If-None-Match`. Their ETag
   is derived only from authorization-independent bracket state. A caller who
   loses access therefore receives the current `401`/`403` response rather
   than a `304`, with no private ETag or cache headers attached.

6. `max_uses`, `use_count`, and `remaining_uses` remain response/model
   compatibility fields pending a reviewed removal migration. They are
   explicitly non-authoritative. `TournamentInviteResponse.is_active` reflects
   only revocation and expiry so it cannot contradict valid bearer access.

## Consequences

The private bearer contract is narrower and reviewable: summary/workspace,
roster, matches and bracket are the only anonymous/inactive code surfaces.
The compatibility invite POST remains read-only for anonymous/new users, while
inactive participants are rejected before any claim or membership operation.
Operators retain revocation and expiry as the durable controls; the bounded
HMAC rate limit reduces direct guessing without making ordinary cookie-only
reads dependent on Redis. Existing clients must stop assuming that a workspace
response contains a default invite code and should use the explicit invite
management/claim response when they need to share a code.
