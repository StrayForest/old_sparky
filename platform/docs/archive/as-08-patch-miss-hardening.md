# AS-08 unknown-patch refresh hardening — closed

- Status: Archived / resolved
- Closed: 2026-08-21
- Implementation pull request: `#6`
- Runtime implementation merge: `41bfa0fd467d3574b9b59863d85b75c580e10616`
- Pull-request security/build verification run: `32494806667`
- Verified `dev` security/build run: `32495100764`
- Production deployment run: `32495372077`
- Verified production release: `gha-32495372077-1-41bfa0fd467d-20260821T150252Z`
- Alembic head: `20260813_0038` (unchanged)

## Original finding

The public patch-detail route accepted bounded numeric patch IDs, but a cache miss could synchronously force the broader external-content refresh before returning. An arbitrary unknown public patch ID could therefore make an anonymous request wait on Steam/asset network work and could repeatedly create avoidable upstream load. The miss-triggered HTTP contour also needed explicit redirect and response-size bounds.

## Decision

Unknown public patch IDs no longer perform external refresh work in the request path.

- Positive patch-cache hits remain the fast path.
- Unknown IDs receive a per-patch Redis negative-cache marker for five minutes.
- The first eligible miss may schedule one detached background refresh; a Redis `NX` gate coalesces distinct misses into at most one miss-triggered refresh per minute across API workers.
- The public request returns the existing not-found result without awaiting Steam or asset-catalog network work.
- Miss-triggered upstream GETs run through a dedicated HTTP client that refuses redirects and fails closed once the decompressed response body exceeds 8 MiB.
- Existing cached patch translation and the public response contract remain unchanged.
- Background refresh failures log the exception type rather than exposing upstream exception text through this public path.

This package is intentionally limited to AS-08. AS-11 public worker-error sanitization remains a separate finding and is not treated as closed by this work.

## Verification

Focused regression coverage verifies all AS-08 invariants:

- a positive cache hit does not schedule refresh work;
- the first unknown ID writes a negative-cache marker and schedules background refresh rather than awaiting it;
- repeated misses for the same ID do not schedule duplicate work;
- distinct unknown IDs share one global refresh gate;
- invalid patch IDs do not touch Redis;
- an already structured cached patch does not force asset refresh;
- miss-triggered HTTP redirects are not followed;
- oversized upstream responses are rejected at the byte bound.

Pull request `#6` passed repository security/build run `32494806667`. After merge, exact source commit `41bfa0fd467d3574b9b59863d85b75c580e10616` passed the full `dev` security/build run `32495100764`: backend migrations and unit/integration tests, static/dependency security gates, frontend audit/typecheck/lint/build and Playwright smoke all succeeded.

## Production evidence

Production deployment run `32495372077` checked out exact CI-verified source commit `41bfa0fd467d3574b9b59863d85b75c580e10616`, built and checksum-verified immutable release `gha-32495372077-1-41bfa0fd467d-20260821T150252Z`, installed it and completed release preflight successfully.

The deployed preflight confirmed a fresh restore-verified database backup and Alembic head `20260813_0038`. After restart, `deadlock-api`, `deadlock-worker`, `deadlock-web` and Nginx were active. Direct API live/readiness checks, loopback edge checks and the public `https://old-sparky.com` web/security smoke completed successfully, after which the workflow marked `platform-production-deploy` successful for the verified source SHA.

The independent browsing environment used for this closeout could not resolve the production hostname, so no additional out-of-band HTTP probe is claimed here. Closure relies on the repository CI regressions and the deployment workflow's own post-switch origin/public smoke evidence.

No database schema, authentication/RBAC, Cloudflare, CSP, Nginx policy or product UI behavior was changed by AS-08.

## Remaining scope

AS-02 remains operator-owned Cloudflare Access/MFA verification. AS-09 distributed login guessing protection is the next code-owned hardening target. AS-10 through AS-13 remain separate P2 packages, including AS-11 worker exception sanitization.