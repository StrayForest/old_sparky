# Platform product reference

- Status: Active reference
- Owner: Platform API and web
- Last reviewed: 2026-08-08

## Public surface

- `/`: tournament steps, official Deadlock patches, Old Sparky videos and
  community links;
- `/tournaments`: public catalog, filters and private invite activation;
- `/info`: player guide, rules, FAQ and the only public support contact form;
- `/privacy` and `/terms`: current public legal documents;
- `/platform-ops`: application-protected admin UI.

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
- Organizer scope applies only to owned tournaments. Admin and superadmin
  checks remain application-side even when Cloudflare Access is enabled.

The detailed anonymous/user/organizer/admin/superadmin matrix and known
exceptions live in the [security audit](application-security-audit.md).

## Tournament contract

- Players create private tournaments within the monthly allowance. Public
  creation requires an explicit permission or admin role.
- Registration is solo and uses current profile data, rank/capacity rules and
  an invite/access check for private tournaments.
- Ready-check, captain selection, assignment, roster lock and bracket changes
  are server-owned transitions. The UI never infers permission or state.
- Roster lock atomically creates one active
  `player_tournament_commitments` row per player. A partial unique index
  prevents two active commitments for one user.
- Losing a single-elimination match, terminal tournament state, withdrawal or
  disqualification releases the relevant commitment. A periodic reconciliation
  task repairs stale rows; assignment JSON remains immutable evidence.
- Match scheduling and result changes use tournament locks/revision checks and
  publish bracket events. The public SSE stream is read-only; connection caps
  remain an open security backlog item.

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
