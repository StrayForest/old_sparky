# AS-06 SSE connection pressure — closed

- Status: Archived / resolved
- Closed: 2026-08-21
- Implementation commit: `7a7e6eb86893bbb4cfceb917f3073849145b1e7a`
- Verified source commit: `84d2ffae41f042237f44874d5509cf3f1e1b162f`
- Security/build verification run: `32475299168`
- Production deployment run: `32475555406`
- Verified production release: `gha-32475555406-1-84d2ffae41f0-20260821T110554Z`
- Alembic head: `20260813_0038` (unchanged)

## Original finding

Public tournament bracket SSE connections were long-lived and had no hard application-owned or origin-owned concurrency boundary. A single source could therefore create enough concurrent streams to consume file descriptors, API concurrency, Redis/pubsub resources or memory and degrade service for legitimate users.

The finding required bounded pressure per source/user and globally, deterministic cleanup after disconnect/timeout, bounded reconnect behavior and observable rejection.

## Decision

SSE pressure is controlled in two independent layers rather than relying on one proxy or one process-local counter.

### Application admission

`SseConnectionLimitMiddleware` protects the bracket-event GET route before the stream is opened.

- Redis sorted-set leases enforce atomic global and source-address admission before authentication lookup.
- Authenticated sessions add a user-scoped lease so one account cannot bypass its limit by changing source addresses.
- Current application ceilings are 128 concurrent SSE streams globally, 6 per source address and 4 per authenticated user.
- Capacity rejection returns HTTP 429 with `Retry-After` and `Cache-Control: no-store`, and logs the rejected scope with a keyed source fingerprint rather than the raw address.
- Redis admission failure is fail-closed as HTTP 503 with the same bounded retry hint; losing the limiter backend cannot silently disable protection.
- Normal request termination releases the lease immediately. Each lease also has bounded expiry, so abnormal process/client termination cannot reserve capacity indefinitely.

### Nginx origin guard

Nginx keeps a second, coarser connection ceiling after Cloudflare real-IP restoration:

- 8 concurrent SSE connections per source address;
- 160 concurrent SSE connections globally for the server;
- connection-limit rejection uses HTTP 429 and is visible through `limit_conn_status` in the structured access log;
- SSE proxy buffering/cache remain disabled, connect/send timeouts are bounded, and read timeout is 60 seconds.

The Nginx ceilings intentionally sit above the application ceilings: the API remains the primary policy layer and can emit structured scope-aware rejection, while Nginx still provides an independent resource-exhaustion backstop.

### Stream lifecycle and reconnect behavior

Bracket streams are capped at 600 seconds. Keepalive opportunities occur every 15 seconds, which keeps healthy connections inside the Nginx read timeout. Each new stream advertises an SSE `retry` value with 5–12 second jitter so routine lifetime rotation or transient disconnects do not cause synchronized reconnect bursts.

The existing private-workspace authorization recheck remains active during the stream and is independent of the connection-pressure limiter.

## Verification

The dedicated Redis/Nginx regression suite covers the required pressure invariants:

- concurrent reservations from one source are atomically bounded;
- the global ceiling is shared across distinct source addresses;
- the authenticated-user ceiling spans distinct source addresses;
- explicit release immediately returns capacity;
- an expired crash lease is pruned before the next capacity check;
- the Nginx contract retains per-source/global connection zones, 429 rejection, structured `limit_conn_status` logging and the bounded SSE read timeout.

The supported security workflow runs the entire backend `unittest` discovery suite against real PostgreSQL and Redis services, followed by dependency/static security gates, frontend audit/typecheck/lint/build and Playwright smoke. Security/build run `32475299168` completed successfully for verified source commit `84d2ffae41f042237f44874d5509cf3f1e1b162f`, which contains implementation commit `7a7e6eb86893bbb4cfceb917f3073849145b1e7a`.

## Production evidence

Production deployment run `32475555406` checked out exact source commit `84d2ffae41f042237f44874d5509cf3f1e1b162f`, built and checksum-verified immutable release `gha-32475555406-1-84d2ffae41f0-20260821T110554Z`, installed it, applied/reloaded the release Nginx configuration and completed preflight plus origin/public smoke successfully. API, worker, web and Nginx were active after restart; Alembic remained at `20260813_0038`.

The production smoke traversed the bracket SSE route as part of the edge-security checks without deliberately saturating production connection capacity. Saturation behavior is covered deterministically by the Redis concurrency tests and Nginx configuration contract rather than by resource-exhaustion traffic against the live service.

No Cloudflare, Turnstile, CSP, application RBAC or authentication control was weakened.

## Remaining scope

AS-02 remains operator-owned Cloudflare Access/MFA verification for privileged routes. AS-07 is the next code-owned hardening package and remains destructive-approval gated. SSE ceiling changes are operational capacity decisions and require retained load/resource evidence rather than ad-hoc increases.
