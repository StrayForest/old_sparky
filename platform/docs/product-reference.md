# Platform product reference

- Status: Active reference
- Owner: Platform API and web
- Last reviewed: 2026-09-13

## Public surface

- `/`: tournament steps, official Deadlock patches, Old Sparky videos and
  community links;
- `/tournaments`: public catalog, filters and private invite activation;
- `/info`: player guide, rules, FAQ and the only public support contact form;
- `/privacy` and `/terms`: current public legal documents;
- `/platform-ops`: Cloudflare Access/MFA-protected and application-protected
  admin UI; application RBAC remains authoritative.

The product UI is Russian-only. The support recipient is backend configuration:
its address must not appear in HTML, metadata, `security.txt`, RSC payloads or
client bundles. `/.well-known/security.txt` points to the HTTPS support form.

## Account and access

- Registration creates an ordinary active player flow and never grants admin
  roles. Email verification and password reset use six-digit, ten-minute,
  one-time codes.
- Sessions use a Secure, HttpOnly, host-only `__Host-` cookie with SameSite Lax.
  Unsafe cookie requests require same-origin evidence and a CSRF token.
- Profile, password, avatar, contacts, captain preferences and dream slots are
  owned by the authenticated account. New-password confirmation is an
  independent browser field; only the password manager may fill both.
- Anonymous public profiles use an explicit public DTO. Account/contact email,
  SteamID and Steam-link state stay outside that contract; user-entered public
  fields such as Discord and region remain visible. A future public email
  feature requires a separate explicit opt-in instead of reusing account data.
- Organizer scope applies only to owned tournaments. Admin and superadmin
  checks remain application-side even when Cloudflare Access is enabled.
- Admin roster operations are intent-specific: the control center separates
  roster correction from lifecycle, role, and destructive cleanup policies;
  each operation rechecks application RBAC and the tournament workflow state.

The detailed anonymous/user/organizer/admin/superadmin matrix and known
exceptions live in the [security audit](application-security-audit.md).

## Tournament contract

- Players create private tournaments within the monthly allowance. Public
  creation requires an explicit permission or admin role.
- Registration is solo and uses current profile data, rank/capacity rules and
  the invite code supplied for private tournaments. A valid nonrevoked,
  nonexpired code is an unlimited, non-consuming bearer read capability for
  the private summary, workspace, participant roster, matches and bracket
  GETs. It creates no account, access record, claim, membership or participant
  row; joining remains an authenticated write with its own workflow checks.
- The bearer capability is a closed API allowlist. It never authorizes profile
  reads, invite management, Ready Check/captain/assignment workflow reads, or
  any POST, PATCH or DELETE. The separate legacy `POST
  /api/v1/tournaments/invites/claim` is only a read-only compatibility
  envelope for body-code lookup; it does not create access, consume a use or
  create membership, and it does not expand the bearer allowlist. Retained
  inactive participants may use the five allowlisted reads with a valid code
  but remain denied on all other private tournament surfaces. Closed and
  completed tournaments remain readable with a valid code; writes remain
  governed by their workflow state.
- Workspace responses omit a default/discovered invite code. When a request
  supplies a valid code, the response may echo only that exact validated code;
  it never returns a different first unrevoked invite. Invite
  `max_uses`/`use_count`/`remaining_uses` fields are compatibility metadata,
  not bearer authorization or consumption state.
- Custom invite codes at tournament creation and invite-code status lookup use
  the same strict raw-code validator: exactly 10–24 ASCII alphanumeric
  characters, canonicalized to uppercase. Punctuation, Unicode, control,
  whitespace, duplicate bearer query values and wrong lengths are rejected
  with a generic `422` before persistence; the API error never echoes the raw
  credential. Auto-generated codes use this same invariant.
- Public participant and workspace payloads use the public roster DTO and do
  not expose moderation notes, moderator identity or moderation timestamps.
  Organizer-only participant management uses a separate management DTO that
  retains those fields.
- Ready-check, captain selection, assignment, roster lock and bracket changes
  are server-owned transitions. The UI never infers permission or state.
- The hidden legacy captain `respond`, `close` and `finalize` routes remain
  compatibility writers only; each takes the tournament workflow lock and
  revalidates the round/participant state before committing. Automation and
  the visible flow remain authoritative for new captain selection.
- Locking a published Deadlock assignment is one atomic handoff: it
  materializes the current teams and members, activates captain/player
  commitments, marks the assignment run locked, seeds the complete bracket
  graph and advances the bracket revision once. The transaction locks the
  tournament first, then the assignment run and its workflow rows in stable
  order. Repeating the organizer lock is an idempotent recovery for a legacy
  locked roster with revision `0` and no matches; an existing graph is never
  reseeded or renumbered.
- The manager/admin `matches/seed-opening-round` endpoint remains a guarded
  compatibility/recovery surface for already-locked legacy rosters. A normal
  lock response is already followed by a full bracket; calling the endpoint
  afterward is a duplicate-seed conflict.
- Roster lock atomically creates one active
  `player_tournament_commitments` row per player. A partial unique index
  prevents two active commitments for one user.
- Losing a single-elimination match, terminal tournament state, withdrawal or
  disqualification releases the relevant commitment. A periodic reconciliation
  task repairs stale rows; assignment JSON remains immutable evidence.
- Match scheduling and result changes use tournament locks/revision checks. The
  bracket grid is request-driven: the initial workspace carries the full
  bracket, explicit mutations may refetch their authoritative result, and
  passive changes become visible after a manual page reload.
- Public and personal tournament cards are served from the rebuildable
  `tournament_list_read_models` projection with indexed filters and cursor/keyset
  pagination. Source tables remain authoritative; `/tournaments/mine` is
  private and uncached.

Workflow changes require the dedicated workflow guardrails and role-matrix
regression coverage.

## Public content

The API fetches official Steam news for Deadlock app `1422450`, sanitizes it to
bounded structured text and never renders source HTML. Patch detail order is:
general, Urn, Unstable Rift, item categories/cost, then heroes. Hero, item,
ability, rank and objective assets are accepted only from their explicit source
allowlists; invalid or unavailable data retains the last safe cache/fallback.

Home summaries use a bounded Redis fresh/stale cache. Patch details and asset
catalogs have versioned keys. YouTube discovery returns at most four regular
channel videos and validates thumbnail hosts. External failures must not make
the home page unavailable.

## Support delivery

`POST /api/v1/content/support/messages` validates a reply address, category and
10–1000-character message, applies Redis rate limiting and a honeypot, and sends
directly through the configured mail provider. Message content is not stored in
PostgreSQL, Redis, audit payloads or application logs. Redis holds only a
short-lived hashed-client counter.

`GET /api/v1/content/support/status` disables the form when delivery is not
configured. The browser never receives the configured recipient address.
