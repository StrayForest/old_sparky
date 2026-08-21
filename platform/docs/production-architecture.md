# Platform production architecture

- Status: Active reference
- Owner: Platform maintainers
- Last reviewed: 2026-08-16

## Invariants

- Platform code writes only to `platformdb`, schema `platform`.
- Secrets, TLS, staging, runtimes, backups and reports live under `/opt/oldsparky/platform/shared`, outside immutable releases.
- Nginx is the only public origin listener. Application/data services bind loopback.
- PostgreSQL is authoritative for durable state; Redis owns bounded ephemeral state, locks, cache and Celery transport; R2 is not a database.

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

## Media boundary

Uploads stream into bounded private staging. The worker decodes, validates, normalizes and re-encodes WebP variants, writes immutable R2 keys, commits metadata and removes staging. Public R2 contains no new originals. API serialization builds CDN descriptors from DB rows and makes no render-path S3 read.

## Trust boundaries

- Only managed Cloudflare ranges may supply `CF-Connecting-IP`; FastAPI accepts proxy headers only from loopback.
- Cookie mutations require application CSRF controls even behind Cloudflare.
- R2, DB, mail, session and Turnstile secrets are backend-only.
- The current shared Unix identity/full env across web/API/worker violates the intended least-privilege boundary and is the next roadmap item.
- The public media bucket and private backup bucket/tokens are separate.

## Release and recovery

`platform_build_release.sh` creates a checksummed immutable artifact. `platform_release_install.sh` expands it under `releases/` and atomically moves `current`/`previous`. DB changes are expand-first. Rollback switches code and never downgrades Alembic automatically.

Daily maintenance restore-verifies DB backups before pruning known artifacts. Off-host encrypted backup remains an operator gate until separate bucket/token and offline key-recovery evidence exist.

## Capacity boundary

The current VPS has two CPU cores and about 3.7 GiB RAM. Image concurrency starts at one. Additional workers, exporters, transforms, poolers or nodes require retained CPU/RSS/queue/DB evidence against the operations targets.
