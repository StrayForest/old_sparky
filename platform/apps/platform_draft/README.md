# Old Sparky Draft

Standalone public Deadlock picks/bans tool for `https://old-sparky.com/draft`.

## Boundary

- `/draft*` is served by Cloudflare, not the Old Sparky VPS.
- Static UI has no Next.js/FastAPI/auth dependency.
- Solo mode is browser-only.
- Online mode uses Cloudflare Durable Objects plus WebSocket Hibernation.
- Online rooms start in a waiting lobby. Each captain claims a seat, may rename
  that team, and must press Ready; drafting starts only after both connected
  captains are ready.
- Standard room creation exposes team format, bans per team (0–3), first mover,
  finite timer (30/45/60/90 seconds) and an editable pick/ban sequence. The
  selected format and ban count are per-team quotas shown beside the two
  sequence rows. Manual edits cannot exceed those quotas, and the Worker
  validates the submitted sequence again before creating the room.
- A turn deadline always advances the authoritative sequence with the first
  unused hero as an automatic pick or ban. There is no pause state.
- A completed draft stays on its current room or Solo screen. Draft does not
  create a separate result route or shareable result link.
- No PostgreSQL, Redis, Celery or durable draft history.
- Active room storage exists only so hibernated WebSockets can resume; it is deleted when a room completes or expires.

## Hero media

The browser reads immutable optimized hero thumbnails from:

`https://cdn.old-sparky.com/draft/heroes/v1/<hero>.webp`

Generate/upload those objects with `platform/tools/sync_draft_hero_assets.py` before first production publication.

## Advertising

The static page uses the existing AdSense publisher `ca-pub-7185165276065459`, matching the root `ads.txt`. Ad placement is kept outside the hero grid/action controls. If explicit ad-unit slots are later preferred over Auto Ads, configure them without changing room/runtime state.

## Cloudflare deploy

The repository workflow expects GitHub Actions secrets:

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`

The token needs the minimum permissions required to deploy the Worker/Durable Object and manage the route for `old-sparky.com`.
Mutable Draft UI assets are sent with `no-store` cache headers so a previous CDN copy cannot keep an old interface live. Hero objects remain immutable under their versioned R2 namespace.

Local development/deploy uses Wrangler, for example:

```bash
cd platform/apps/platform_draft
npx wrangler dev
npx wrangler deploy
```

Normal production publication should use the repository workflow rather than an operator workstation.
