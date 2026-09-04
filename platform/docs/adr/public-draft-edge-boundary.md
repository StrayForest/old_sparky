# Public Draft edge boundary

- Status: Accepted
- Date: 2026-09-04
- Owner: Platform maintainers

## Context

Old Sparky needs a public Deadlock picks/bans tool that is not part of a tournament, does not require an account and does not need durable history. The existing platform VPS is intentionally capacity-managed around the Next.js, FastAPI, PostgreSQL and Redis workloads, so adding long-lived realtime connections there would consume origin resources for a feature that has no persistence requirement.

The site already uses Cloudflare in front of `old-sparky.com` and delivers public media through the `R2 -> CDN -> browser` boundary.

## Decision

`https://old-sparky.com/draft*` is a separate deployment boundary served by Cloudflare rather than the Old Sparky VPS.

- The main Next.js site exposes `Драфты` immediately before `Инфо` and performs a full-document navigation to `/draft`.
- Draft does not render the platform SiteHeader, SiteFooter or authentication providers.
- Static Draft HTML/CSS/JavaScript is served with Cloudflare Static Assets.
- Solo drafts are browser-only.
- Each online room maps to one Cloudflare Durable Object and uses WebSocket Hibernation for realtime captain/spectator transport.
- Online rooms expose a waiting lobby where both connected captains claim their
  seats, can rename their own teams and confirm readiness before the first
  draft step starts.
- Room creation carries an explicitly validated team format, per-team ban
  template (0–3), first mover, timer and complete pick/ban sequence. The
  template is editable: the resulting sequence is authoritative, with at most
  three bans per team and no gaps. A server-authoritative deadline
  automatically applies the first unused hero for the current pick or ban;
  timeout never pauses a room.
- The Durable Object stores only the small current room snapshot and captain credential hashes needed to resume a hibernated room. It does not keep draft history. Completed and expired rooms delete this state.
- Draft does not use platform PostgreSQL, Redis, Celery, FastAPI or the VPS runtime.
- Hero thumbnails use the existing `cdn.old-sparky.com` R2/CDN boundary under an immutable Draft namespace.
- A completed result can be encoded into the URL fragment of `/draft/result`; the server does not store that result.
- Draft advertising uses the existing Old Sparky AdSense publisher and stays outside the hero grid and irreversible-action controls.

## Consequences

- Draft traffic and idle realtime connections do not consume Old Sparky VPS CPU, database connections or Redis capacity.
- Active online rooms are intentionally ephemeral. A Cloudflare room expiry or incompatible deployment can invalidate an unfinished room.
- The room URL is public spectator identity, not mutation authority. Captain authority uses separate high-entropy secrets; the guest secret is transferred in a URL fragment and removed from the visible URL after the browser captures it.
- `/draft` is indexable, while ephemeral room/result routes are `noindex`.
- Cloudflare Worker/Durable Object deployment requires operator-managed Cloudflare credentials in the release workflow; secrets are never committed to Git.
- Mutable Draft UI assets use `no-store` cache headers; hero thumbnails remain immutable under their versioned R2 namespace.
