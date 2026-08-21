# Cloudflare production checklist

- Status: Active operator checklist
- Owner: Cloudflare account owner
- Last reviewed: 2026-08-08

`DONE` requires live/dashboard evidence. `VERIFY` needs dashboard confirmation;
source code alone is not evidence. Never use a Global API Key.

## DNS and certificates

- DONE: apex and `cdn.old-sparky.com` are proxied and serve HTTPS.
- VERIFY: apex origin address remains correct/proxied; no accidental AAAA until
  origin IPv6 is tested.
- TODO: enable DNSSEC and verify DS propagation.
- REVIEW: scanner reports no CAA. Add records only after confirming every CA
  Cloudflare currently needs for Universal/backup certificates; an incomplete
  CAA policy can block renewal.
- VERIFY: edge certificate active and expiry alerts enabled.

## Edge TLS

- VERIFY: encryption mode is **Full (strict)**.
- VERIFY: minimum visitor TLS is 1.2 and TLS 1.3/HTTP3 are enabled.
- OBSERVED: the supplied scan accepts TLS 1.2 ECDHE-CBC compatibility suites.
  Cloudflare documents that its default legacy-compatible edge set can be
  flagged by scanners and that custom edge suites require Advanced Certificate
  Manager. Decide whether the compatibility/paid-feature trade-off justifies
  customization; the origin now has an independent AEAD-only allowlist.
- VERIFY: public responses currently advertise HSTS `max-age=15552000`, while
  one supplied snapshot reports no HSTS. Confirm hostname/date/cache and the
  dashboard owner before any change. Do not add a second origin header.
- INFO: no OCSP stapling/HPKP result is not an application change request.
  HPKP is intentionally not used; Cloudflare owns edge certificate status.

## Cache and R2

- VERIFY: public media bucket is Standard, `r2.dev` is disabled and browser PUT
  CORS is absent.
- VERIFY: media token is bucket-scoped and not reused for backups.
- VERIFY: cache rule applies only to `cdn.old-sparky.com`, respects immutable
  origin Cache-Control/query keys and does not cache 4xx/5xx.
- TODO: purge/revalidate pre-2026-08-08 `old-sparky.com/assets/*` responses that
  retain pre-security-header metadata.
- DO NOT ENABLE without a measured need: Cache Reserve, Images transforms,
  Rocket Loader or Cache Everything on the apex.

## Security and admin edge

- TODO: enable/tune Managed WAF after observing false positives.
- TODO: add bounded edge rates for register, login, reset, invite, support and
  upload; application controls stay authoritative.
- VERIFY: Turnstile hostname allowlist contains only production hostnames.
- TODO/P1: Access + MFA for `/platform-ops*`, `/api/v1/admin*` and audited
  security paths; retain application RBAC and a tested break-glass/rollback.
- VERIFY: Bot Fight Mode does not break API, health, Turnstile, CDN or crawlers.

## Origin protection

- DONE: UFW permits 80/443 only from managed Cloudflare IPv4/IPv6 ranges and
  retains SSH/out-of-band console access.
- VERIFY: daily range update and UFW parity alert; add ranges before removing
  old ones.
- OPTIONAL: Authenticated Origin Pulls is later defense-in-depth and does not
  replace Full(strict).

References: [Cloudflare cipher suites](https://developers.cloudflare.com/ssl/edge-certificates/additional-options/cipher-suites/),
[minimum TLS](https://developers.cloudflare.com/ssl/edge-certificates/additional-options/minimum-tls/),
[R2 cache](https://developers.cloudflare.com/cache/interaction-cloudflare-products/r2/).
