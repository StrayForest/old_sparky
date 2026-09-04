# Standalone Draft Agent Guide

## Scope

- This subtree is the standalone public Deadlock draft tool served at `https://old-sparky.com/draft*` by Cloudflare.
- It is intentionally isolated from the main Next.js/FastAPI runtime. Do not add imports or runtime dependencies on tournaments, auth, PostgreSQL, Redis or Celery.
- The normal Old Sparky header/footer do not render inside Draft. The main site only links to `/draft`.

## Runtime

- Static HTML/CSS/JavaScript is served by Cloudflare Static Assets.
- Online rooms use one Durable Object per room and WebSocket Hibernation. Room state is ephemeral and deleted after completion/expiry.
- Solo mode is browser-only.
- Hero thumbnails are immutable public media under the existing `cdn.old-sparky.com` R2/CDN contour.
- Do not add a framework or package dependency unless the plain platform APIs are insufficient.

## Product and UX

- Russian user-facing copy.
- Current Old Sparky modern palette: dark navy surfaces, purple primary and teal secondary. Do not introduce a gold theme.
- Pick/ban actions require explicit selection then confirmation.
- Keep the hero pool usable on mobile and avoid horizontal overflow.
- Advertising must stay outside the interactive hero grid/action controls.

## Security and limits

- Public room codes are not credentials. Captain authority uses high-entropy secrets.
- Validate all messages and room rules server-side and bound message size, team names, sequence length and spectator count.
- Never log captain/join secrets.
