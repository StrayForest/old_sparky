# Platform product reference

- Status: Active reference
- Owner: Platform API and web
- Last reviewed: 2026-09-01

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
  the invite code supplied for private tournaments. Entering a code only opens
  the private tournament workspace; it creates no account, access record or
  participant row.
- Public participant and workspace payloads use the public roster DTO and do
  not expose moderation notes, moderator identity or moderation timestamps.
  Organizer-only participant management uses a separate management DTO that
  retains those fields.
- Ready-check, captain selection, assignment, roster lock and bracket changes
  are server-owned transitions. The UI never infers permission or state.
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
