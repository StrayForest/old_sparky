# Platform production architecture

- Status: Active reference
- Owner: Platform maintainers
- Last reviewed: 2026-08-21

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
- Public bracket SSE uses layered admission protection: Redis-backed application leases bound global, source and authenticated-user concurrency, while Nginx retains an independent coarse source/global connection ceiling.
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

The platform connects directly to PostgreSQL with explicit API, worker and SSE
pool limits: the measured 10k browser-polling baseline reserves `2 x (16 + 0)`
API connections, `2 x (2 + 0)` worker connections and `2 x 2` separate SSE
authorization-pool connections within a 40-connection budget. This is a
bounded increase, not unlimited overflow; add a database pooler only from new
measured scaling evidence. High-volume optional-authenticated reads validate
the session in their request transaction but do not perform a second
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
- Nginx owns origin TLS, proxy limits, cache headers and browser-hardening headers for HTML, API, static, SSE and error responses.
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

FastAPI exposes no `/api/v1/uploads/*` media-serving route and has no uploads `StaticFiles` mount. Normal runtime storage has no object-read API, R2 `get_object()`/`Body.read()` proxy path or R2-to-local-disk read fallback. Historical `avatar_url`, `banner_url` and `cover_url` fields may remain during the grace period, but runtime resolution ignores them; only ready `MediaDescriptor` URLs can reach public serialization. Legacy upload-URL parsing and object mutation helpers are migration/grace-period tooling only and must not become browser delivery paths.

## Trust boundaries

- Only managed Cloudflare ranges may supply `CF-Connecting-IP`; FastAPI accepts proxy headers only from loopback.
- Cookie mutations require application CSRF controls even behind Cloudflare.
- Anonymous public response DTOs are explicit schema allowlists: account/contact email and Steam authentication identity do not cross the public-profile boundary, while participant moderation note, moderator identity and moderation timestamps are restricted to the organizer-management DTO.
- Invite-only tournament workspace reads (`workspace`, roster, matches, bracket and bracket SSE admission) require active participant membership or explicit organizer/admin authority; retained `withdrawn`/`disqualified` participant rows are historical and grant no workspace access.
- SSE admission state is ephemeral Redis state. Admission fails closed when that state cannot be consulted; normal termination releases the lease immediately, and bounded lease expiry recovers capacity after abnormal process/client termination.
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

The current VPS has two CPU cores and about 3.7 GiB RAM. Image concurrency starts at one. Public bracket SSE application admission is capped at 3,000 streams globally, 32 per source address and 4 per authenticated user; Nginx retains coarser 10,240 source/global ceilings. Individual streams are bounded to 600 seconds, send keepalive opportunities every 15 seconds and advertise reconnect delay with 5–12 second jitter. Visible bracket tabs use SSE, while hidden tabs and admission failures fall back to revision polling. Stream admission and private-stream revalidation DB work are bounded per API worker so an open/event fan-out cannot consume the entire ordinary-request DB pool. These values are capacity safeguards, not product entitlements; change them only from retained load/resource evidence.

Additional workers, exporters, transforms, poolers or nodes require retained CPU/RSS/queue/DB evidence against the operations targets.
