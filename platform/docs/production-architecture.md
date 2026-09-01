# Platform production architecture

- Status: Active reference
- Owner: Platform maintainers
- Last reviewed: 2026-09-01

## Invariants

- Platform code writes only to `platformdb`, schema `platform`.
- Secrets, TLS, staging, runtimes, backups and reports live under `/opt/oldsparky/platform/shared`, outside immutable releases.
- Nginx is the only public origin listener. Application/data services bind loopback.
- PostgreSQL is authoritative for durable state; Redis owns bounded ephemeral state, locks, cache and Celery transport; R2 is not a database.
- Tournament invite-use and participant-capacity decisions are serialized in
  PostgreSQL: invite claim/revoke locks the tournament row and then the invite
  row, ordinary joins claim durable per-tournament slots with
  `FOR UPDATE SKIP LOCKED`, and inactive restoration retains the lifecycle
  lock before reactivation. The unique participant key and slot table remain
  authoritative; Redis is not the capacity store. The slot table keeps a
  bounded free-slot inventory and allocates sparse slot rows on demand above
  the inventory window, so a large advertised capacity never materializes
  millions of rows.
- Ready Check is time-based. The tournament workspace sends
  `ready_check_starts_at`, `ready_check_ends_at` and a UTC `server_time` anchor;
  the browser derives a server-relative monotonic timer and changes the button
  locally at the two boundaries. The vote POST remains server-authoritative
  and validates the schedule, participant, eligibility and workflow state;
  delayed automation cannot reject a valid in-window vote. The bracket grid is
  also request-driven: the initial workspace contains the full bracket, passive
  changes appear after a manual page reload, and explicit bracket mutations may
  refetch their authoritative response. Redis remains available to unrelated
  platform services. See [the timing and bracket boundary ADR](adr/ready-check-and-bracket-boundary.md).
- Public media rendering is one-way `R2 -> CDN -> browser`; the API does not proxy media object bytes or fall back to local-disk reads.

## Request and data flow

```text
Browser
  -> Cloudflare (DNS, edge TLS, cache, WAF, Turnstile)
     -> HTTPS Full(strict)
        -> Nginx :443
           -> Next.js standalone 127.0.0.1:3000
           -> FastAPI/Gunicorn 127.0.0.1:8010
              -> PostgreSQL 127.0.0.1:5432
              -> Redis 127.0.0.1:6379
              -> Celery worker through Redis
              -> R2 S3 API for prepared-media mutations

Browser -> cdn.old-sparky.com -> Cloudflare cache -> public R2 variants
```

Public catalog requests use a short origin cache: the API may serve an
anonymous query representation from Redis for five seconds, and Nginx emits
`public, max-age=5, s-maxage=15, stale-while-revalidate=30` for the exact
catalog path. Cloudflare edge caching is prepared but remains operator-owned:
the exact-path Cache Rule is still required and the 2026-09-01 live probe saw
`CF-Cache-Status: DYNAMIC`. The cache is keyed only by normalized public
filters, limit and cursor; Redis failures are treated as misses. Personal
`/tournaments/mine` responses are private and never enter either response
cache.

The catalog list path reads the rebuildable PostgreSQL
`tournament_list_read_models` projection. Its rows contain the card fields,
organizer metadata, participant count and locked-roster flag, so the hot read
does not join profiles or aggregate participant/assignment rows. PostgreSQL
remains the source of truth. Committed tournament, participant, workflow,
profile and media mutations run best-effort post-commit refresh hooks; the
Alembic backfill and bounded repair service provide recovery if a hook is
interrupted. The read query applies indexed filters, keyset predicates and
`LIMIT + 1` to derive `has_more` without an exact total count.

### Catalog plan evidence

On 2026-09-01, a disposable 20,000-row projection fixture was used for bounded
`EXPLAIN (ANALYZE, BUFFERS)` checks. The default public page used
`ix_tournament_list_public_created_at_id` without a sort (about 0.083 ms);
participant-desc sorting used its projection index without a sort (about
0.062 ms); the organizer `/mine` path used
`ix_tournament_list_organizer_created_at_id` without a sort (about 0.197 ms);
and lower-name search used a bitmap scan on the trigram index
`ix_tournament_list_name_lower_trgm`. These are plan-shape checks on a
disposable synthetic fixture, not production latency SLOs. Re-run them against
production-sized data before changing index or projection strategy.

The platform connects directly to PostgreSQL with explicit API and worker pool
limits within the ordinary connection budget. High-volume
optional-authenticated reads validate the session in their request transaction
but do not perform a second
`last_seen_at` write transaction; that metadata is not an authorization
decision and must not double the connection demand of a read burst.

## Component ownership

- Next.js owns presentation, responsive behavior and browser API calls. It does not own authorization or tournament transitions.
- FastAPI owns HTTP schemas, authentication context and adapter orchestration.
- Domain/services own permissions, workflow invariants and concurrency rules.
- SQLAlchemy/Alembic own persistence and expand/contract schema evolution.
- Celery owns image processing, assignment compute, reconciliation and
  external refresh work that must not block HTTP. Existing Redis transport is
  split into high/default/low queues with prefetch-one and late acknowledgements
  so workflow work has priority over cleanup/refresh backlog.
- Nginx owns origin TLS, proxy limits, cache headers and browser-hardening headers for HTML, API, static and error responses.
- Cloudflare owns public DNS/edge TLS/HSTS/cache/WAF/Access. Edge controls never grant an application role.

## Runtime identity and credential boundary

- Next.js runs as `oldsparky-web`, FastAPI as `oldsparky-api`, and Celery as `oldsparky-worker`; each has a private primary group.
- `/opt/oldsparky/platform/shared/.env.platform` is the root-owned canonical operator source and is not readable by runtime identities.
- Deployment renders least-privilege service inputs under `/opt/oldsparky/platform/shared/env/`; each unit loads only its own runtime env.
- The web runtime receives no database, session-signing, R2 secret, mail-delivery, Turnstile secret or OpenAI credential.
- API and worker share only the `oldsparky-media` supplementary group required for media staging. Worker scratch space and web cache remain service-owned.
- systemd process visibility is restricted with `ProtectProc=invisible`; runtime scripts fail closed if a service is started with the wrong service identity/environment contract.

## Media boundary

Uploads stream into bounded private staging. The worker decodes, validates, normalizes and re-encodes WebP variants, writes immutable R2 keys, commits metadata and removes staging. Public R2 contains no new originals. API serialization builds CDN descriptors from committed media rows and makes no render-path S3 read.

FastAPI exposes no `/api/v1/uploads/*` media-serving route and has no uploads `StaticFiles` mount. Normal runtime storage has no object-read API, R2 `get_object()`/`Body.read()` proxy path or R2-to-local-disk read fallback. Historical `avatar_url`, `banner_url` and `cover_url` fields remain only as API/data-migration compatibility fields; runtime resolution ignores their stored values, and only ready `MediaDescriptor` URLs can reach public serialization. Legacy upload-URL parsing and object mutation helpers are migration tooling for existing data only and must not become browser delivery paths. Physical field removal requires a reviewed API/schema migration after data and consumer inventory.

## Trust boundaries

- Only managed Cloudflare ranges may supply `CF-Connecting-IP`; FastAPI accepts proxy headers only from loopback.
- Cookie mutations require application CSRF controls even behind Cloudflare.
- Anonymous public response DTOs are explicit schema allowlists: account/contact email and Steam authentication identity do not cross the public-profile boundary, while participant moderation note, moderator identity and moderation timestamps are restricted to the organizer-management DTO.
- Invite-only tournament workspace reads (`workspace`, roster, matches and bracket) require active participant membership or explicit organizer/admin authority; retained `withdrawn`/`disqualified` participant rows are historical and grant no workspace access.
- Ready Check timing is carried by the authenticated/eligible tournament
  workspace response and is not an authorization grant. The browser timer is
  presentation-only; the vote transaction checks server time and all durable
  eligibility/workflow rules, and the worker may remain responsible for
  timeout/no-show and later workflow side effects. The explicit Ready Check
  state read remains available for workspace, organizer/admin and recovery
  flows, but it is not polled to discover a known timestamp.
- R2, DB, mail, session and Turnstile secrets are backend-only and are not present in the web runtime environment.
- The public media bucket and private backup bucket/tokens are separate.

## Release and recovery

The GitHub production workflow builds, publishes and attests the immutable
release artifact and its hash-locked wheelhouse. The VPS verifies the
published digest/source commit and installs that artifact; it does not resolve
dependencies or build from a source checkout.
`platform_release_deploy.sh` stages it, makes the migration decision, then
retains a durable receipt through pointer activation, runtime readiness, Nginx
apply and smoke before committing. DB changes are expand-first. Rollback
switches code and never downgrades Alembic automatically. See
[`release-state-machine.md`](release-state-machine.md).

Daily maintenance restore-verifies DB backups before pruning known artifacts. Off-host encrypted backup remains an operator gate until separate bucket/token and offline key-recovery evidence exist.

## Capacity boundary

The current VPS has two CPU cores and about 3.7 GiB RAM. Ready Check and the
bracket grid are request-driven: waiting users create no Ready Check network
traffic, and the `starts_at`/`ends_at` transition creates no request. The
replacement production QA is a short Ready vote burst that measures POST
latency, accepted/rejected reasons, duplicate/idempotency behavior, database
pool wait and locks, and API/PostgreSQL CPU and connections.

Additional workers, exporters, transforms, poolers or nodes require retained CPU/RSS/queue/DB evidence against the operations targets.
