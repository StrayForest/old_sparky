# Platform production architecture

- Status: Active reference
- Owner: Platform maintainers
- Last reviewed: 2026-08-21

## Invariants

- Platform code writes only to `platformdb`, schema `platform`.
- Secrets, TLS, staging, runtimes, backups and reports live under `/opt/oldsparky/platform/shared`, outside immutable releases.
- Nginx is the only public origin listener. Application/data services bind loopback.
- PostgreSQL is authoritative for durable state; Redis owns bounded ephemeral state, locks, cache and Celery transport; R2 is not a database.
- Tournament invite-use and active participant-capacity decisions are serialized in PostgreSQL: invite claim/revoke locks the tournament row and then the invite row, while participant-count mutations serialize on the tournament row and inactive restoration rechecks capacity before reactivation.

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

The platform connects directly to PostgreSQL. Add a database pooler only from measured scaling evidence.

## Component ownership

- Next.js owns presentation, responsive behavior and browser API calls. It does not own authorization or tournament transitions.
- FastAPI owns HTTP schemas, authentication context and adapter orchestration.
- Domain/services own permissions, workflow invariants and concurrency rules.
- SQLAlchemy/Alembic own persistence and expand/contract schema evolution.
- Celery owns image processing, assignment compute, reconciliation and external refresh work that must not block HTTP.
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

Uploads stream into bounded private staging. The worker decodes, validates, normalizes and re-encodes WebP variants, writes immutable R2 keys, commits metadata and removes staging. Public R2 contains no new originals. API serialization builds CDN descriptors from DB rows and makes no render-path S3 read.

## Trust boundaries

- Only managed Cloudflare ranges may supply `CF-Connecting-IP`; FastAPI accepts proxy headers only from loopback.
- Cookie mutations require application CSRF controls even behind Cloudflare.
- Invite-only tournament workspace reads (`workspace`, roster, matches, bracket and bracket SSE admission) require active participant membership or explicit organizer/admin authority; retained `withdrawn`/`disqualified` participant rows are historical and grant no workspace access.
- R2, DB, mail, session and Turnstile secrets are backend-only and are not present in the web runtime environment.
- The public media bucket and private backup bucket/tokens are separate.

## Release and recovery

`platform_build_release.sh` creates a checksummed immutable artifact. `platform_release_install.sh` expands it under `releases/` and atomically moves `current`/`previous`. DB changes are expand-first. Rollback switches code and never downgrades Alembic automatically.

Daily maintenance restore-verifies DB backups before pruning known artifacts. Off-host encrypted backup remains an operator gate until separate bucket/token and offline key-recovery evidence exist.

## Capacity boundary

The current VPS has two CPU cores and about 3.7 GiB RAM. Image concurrency starts at one. Additional workers, exporters, transforms, poolers or nodes require retained CPU/RSS/queue/DB evidence against the operations targets.
