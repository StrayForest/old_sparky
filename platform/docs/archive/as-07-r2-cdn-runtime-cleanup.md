# AS-07 R2/CDN runtime media cleanup — closed

- Status: Archived / resolved
- Closed: 2026-08-21
- Runtime implementation commit: `a2292fbf4016b1d8bfab53c2e71a7d06f5c59abe`
- Verified source commit: `d7ade5898b53cc236e78e873237762132347ff6a`
- Pull request: `#3`
- Security/build verification run: `32486516129`
- Production deployment run: `32486778577`
- Verified production release: `gha-32486778577-1-d7ade5898b53-20260821T132644Z`
- Alembic head: `20260813_0038` (unchanged)

## Original finding

The legacy media contour could serve `/api/v1/uploads/*` through FastAPI, read R2 objects back into the API process, fall back from R2 to local-disk reads and allow historical `avatar_url`, `banner_url` or `cover_url` fields to re-enter runtime serialization. That duplicated the intended public-media path, retained whole-object API buffering and kept local storage coupled to normal image delivery.

The target architecture is one-way for public rendering: upload into bounded private staging, validate/process in the worker, persist immutable public variants in R2, return CDN URLs from committed media metadata, and let the browser fetch those variants through Cloudflare CDN.

## Decision

Normal runtime media delivery is now strictly `R2 -> CDN -> browser`.

- FastAPI no longer exposes `/api/v1/uploads/*` and no uploads `StaticFiles` mount remains.
- `ObjectStorage` no longer exposes a runtime `get()` operation, `StoredObject` or local read helper.
- The API no longer calls R2 `get_object()` / `Body.read()` to proxy image bytes to clients.
- There is no R2-to-local-disk runtime read fallback.
- Runtime URL resolution returns only a ready `MediaDescriptor` URL. Historical `avatar_url`, `banner_url` and `cover_url` values are ignored and cannot act as serializer fallbacks.
- Legacy upload-URL parsing plus object `put`/`delete` support remain only for bounded migration/grace-period tooling; they are not a browser delivery path.

## Verification

Regression coverage explicitly seeds historical legacy URL values and verifies that they are not returned when no ready media descriptor exists. Existing pagination, tournament-policy and bracket-flow expectations were updated to the same invariant after the first PR CI run exposed three stale tests that still expected the removed fallback behavior.

PR `#3` completed successfully after those stale expectations were corrected. The subsequent `dev` push security/build run `32486516129` passed backend unit/integration tests, static/dependency security gates, frontend audit/typecheck/lint/build and Playwright smoke for merge commit `d7ade5898b53cc236e78e873237762132347ff6a`.

## Production evidence

Production deployment run `32486778577` checked out exact verified source commit `d7ade5898b53cc236e78e873237762132347ff6a`, built and checksum-verified immutable release `gha-32486778577-1-d7ade5898b53-20260821T132644Z`, installed it and completed preflight plus origin/public smoke successfully.

The deployed preflight confirmed a fresh restore-verified database backup and Alembic head `20260813_0038`. After restart, `deadlock-api`, `deadlock-worker`, `deadlock-web` and Nginx were active; direct API readiness, origin smoke and public `https://old-sparky.com` smoke passed. The production workflow then marked `platform-production-deploy` successful for the verified source SHA.

No Cloudflare, Turnstile, CSP, application RBAC or authentication control was weakened.

## Remaining scope

The historical SQL/schema fields `avatar_url`, `banner_url` and `cover_url` may still be read or passed at some serializer call sites during the grace period, but they are runtime-inert because the resolver ignores them. Their physical removal from SQL queries, call sites, models/migrations and schema is a separate post-grace cleanup and is not required to preserve the closed AS-07 runtime invariant.

The bounded legacy upload-URL parser and object mutation helpers may likewise remain while migration/reconciliation tooling still needs them. They must not be reintroduced into normal render-path reads or browser delivery.

AS-02 remains operator-owned Cloudflare Access/MFA verification. AS-08 is the next code-owned hardening target.
