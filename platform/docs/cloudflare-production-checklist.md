# Cloudflare production checklist

- Status: Active operator checklist
- Owner: Cloudflare account owner
- Last reviewed: 2026-09-05

`DONE` requires live/dashboard evidence. `VERIFY` needs dashboard confirmation;
source code alone is not evidence. Never use a Global API Key. The manual
[`Platform Cloudflare read-only audit`](../../.github/workflows/platform-cloudflare-readonly-audit.yml)
workflow uses the existing GitHub Actions secrets for GET-only API evidence and
stores a redacted report as a short-lived artifact; an API permission failure is
evidence that access is missing, not evidence that the control is configured.

## DNS and certificates

- DONE: apex and `cdn.old-sparky.com` are proxied and serve HTTPS.
- DECISION: `old-sparky.com` is the only supported public hostname. `www.old-sparky.com` intentionally has no DNS alias and must not be introduced as a second canonical origin without an explicit redirect/SEO decision.
- VERIFY: apex origin address remains correct/proxied; no accidental AAAA until
  origin IPv6 is tested.
- DONE: DNSSEC is enabled and the operator confirmed the production DNSSEC/DS
  contour on 2026-08-21.
- REVIEW: scanner reports no CAA. Add records only after confirming every CA
  Cloudflare currently needs for Universal/backup certificates; an incomplete
  CAA policy can block renewal.
- VERIFY: edge certificate active and expiry alerts enabled.

## Edge TLS

- DONE: encryption mode is **Full (strict)**; operator-confirmed in Cloudflare
  on 2026-08-21.
- DONE: minimum visitor TLS is 1.2 and TLS 1.3/HTTP3 are enabled;
  operator-confirmed in Cloudflare on 2026-08-21.
- OBSERVED: the supplied scan accepts TLS 1.2 ECDHE-CBC compatibility suites.
  Cloudflare documents that its default legacy-compatible edge set can be
  flagged by scanners and that custom edge suites require Advanced Certificate
  Manager. Decide whether the compatibility/paid-feature trade-off justifies
  customization; the origin now has an independent AEAD-only allowlist.
- DONE: Cloudflare owns HSTS and the Edge Certificates dashboard showed HSTS
  **On**, `max-age=15552000` (6 months), `includeSubDomains` Off and preload Off
  on 2026-08-21. Nginx must not add a second HSTS header. Closure evidence is in
  [`archive/as-14-cloudflare-hsts-ownership.md`](archive/as-14-cloudflare-hsts-ownership.md).
- INFO: no OCSP stapling/HPKP result is not an application change request.
  HPKP is intentionally not used; Cloudflare owns edge certificate status.

## Cache and R2

- VERIFY: public media bucket is Standard, `r2.dev` is disabled and browser PUT
  CORS is absent.
- VERIFY: media token is bucket-scoped and not reused for backups.
- VERIFY: cache rule applies only to `cdn.old-sparky.com`, respects immutable
  origin Cache-Control/query keys and does not cache 4xx/5xx.
- DONE: public catalog cache behavior is live-proven for
  `old-sparky.com/api/v1/tournaments`: anonymous GETs returned
  `CF-Cache-Status: MISS` followed by `HIT` with `Age: 0`; the actual
  production session cookie `__Host-old_sparky_session` and `Authorization`
  returned `DYNAMIC`; `/api/v1/tournaments/mine` returned `401`,
  `Cache-Control: no-store` and `DYNAMIC`. Closure evidence is in
  [`archive/as-02-cloudflare-catalog-cache-2026-09-05.md`](archive/as-02-cloudflare-catalog-cache-2026-09-05.md).
- VERIFY: capture the Cloudflare Trace/dashboard rule expression to retain
  direct operator evidence for the scope: GET/HEAD HTTP 200 only, full query
  string in the cache key, origin `s-maxage=15` and
  `stale-while-revalidate=30`, bypass for `Authorization` or the production
  session cookie, and no match for `/api/v1/tournaments/mine`, other `/api/`
  routes or HTML. Do not enable Cache Everything on the apex.
- TODO: purge/revalidate pre-2026-08-08 `old-sparky.com/assets/*` responses that
  retain pre-security-header metadata.
- DO NOT ENABLE without a measured need: Cache Reserve, Images transforms,
  Rocket Loader or Cache Everything on the apex.

## Security and admin edge

- TODO: enable/tune Managed WAF after observing false positives.
- TODO: add bounded edge rates for register, login, reset, invite, support and
  upload; application controls stay authoritative.
- VERIFY: Turnstile hostname allowlist contains only production hostnames.
- DONE: Cloudflare Access protects `/platform-ops*` and `/api/v1/admin*` with an
  operator-scoped Allow policy and independent MFA. A TOTP device was enrolled
  and a fresh incognito login verified the identity -> MFA -> application path
  on 2026-08-21; application RBAC remains authoritative. Closure evidence is in
  [`archive/as-02-cloudflare-access-mfa.md`](archive/as-02-cloudflare-access-mfa.md).
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
