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
The full read-only API run on 2026-09-05 produced `9 PASS` and `8 REVIEW`.
The narrow Cache Rule repair then succeeded in remediation run
[`33964594777`](https://github.com/StrayForest/old_sparky/actions/runs/33964594777),
and the follow-up read-only evidence is retained in
[`33964617403`](https://github.com/StrayForest/old_sparky/actions/runs/33964617403).
The durable summary is in
[`archive/as-02-cloudflare-api-audit-2026-09-05.md`](archive/as-02-cloudflare-api-audit-2026-09-05.md).
The follow-up operator run applied the CAA, Certificate Transparency and
plan-compatible login edge-rate controls; evidence and provider rejection
details are in
[`archive/as-02-cloudflare-operator-remediation-2026-09-05.md`](archive/as-02-cloudflare-operator-remediation-2026-09-05.md).

## DNS and certificates

- DONE: the read-only audit confirmed the apex A record and
  `cdn.old-sparky.com` are proxied; no apex AAAA record was returned, and
  `www.old-sparky.com` has no DNS records.
- DECISION: `old-sparky.com` is the only supported public hostname. `www.old-sparky.com` intentionally has no DNS alias and must not be introduced as a second canonical origin without an explicit redirect/SEO decision.
- DONE: DNSSEC is enabled and the operator confirmed the production DNSSEC/DS
  contour on 2026-08-21.
- DONE: apex CAA policy explicitly permits `issue` and `issuewild` for the
  four Cloudflare partner CAs used for Universal/backup certificates; the
  operator run observed all eight records present.
- DONE: the edge certificate API returned an active Universal certificate and
  an issued Let’s Encrypt backup certificate for the production hosts.
- DONE: Certificate Transparency alerting is enabled with the operator email
  destination. The Universal SSL lifecycle alert type was not available in
  the account alert catalog, so provider-plan expiry/lifecycle alerting is a
  remaining review item.

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

- DONE: read-only API evidence confirms the public media bucket is Standard,
  `r2.dev` is disabled and browser PUT CORS is absent. The R2 custom domain
  `cdn.old-sparky.com` is enabled with active ownership/SSL and minimum TLS
  1.2; see [`archive/as-02-cloudflare-api-audit-2026-09-05.md`](archive/as-02-cloudflare-api-audit-2026-09-05.md).
- VERIFY: media token is bucket-scoped and not reused for backups. The checked-
  in runtime contract enforces separate media/backup buckets and credentials,
  but Cloudflare does not expose the secret token’s scope through this API.
- DONE: the reviewed cache ruleset now bypasses the public catalog for
  `Authorization` or the actual `__Host-old_sparky_session` cookie. Anonymous
  catalog requests returned `HIT`, while a session-cookie request returned
  `DYNAMIC`; `/api/v1/tournaments/mine` remained `401`/`no-store`/`DYNAMIC`.
  The API rule definition and live smoke are retained in the two linked runs
  above and in [`archive/as-02-cloudflare-catalog-cache-2026-09-05.md`](archive/as-02-cloudflare-catalog-cache-2026-09-05.md).
- DONE: public catalog cache behavior is live-proven for
  `old-sparky.com/api/v1/tournaments`: warmed anonymous GETs returned
  `CF-Cache-Status: HIT`; the actual
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

- REVIEW: the zone is not entitled to execute the Cloudflare Managed WAF
  ruleset through the API; no managed-WAF rule was created. Revisit after a
  plan/entitlement change.
- PARTIAL: one plan-compatible login edge rate limit is active. The current
  plan exposes one edge rule slot and a ten-second period; application Redis
  controls remain authoritative for register and reset, while invite/support/
  upload have no edge rule in this plan.
- DONE: read-only API evidence shows the active Turnstile widget allowlist is
  only `old-sparky.com`.
- DONE: Cloudflare Access protects `/platform-ops*` and `/api/v1/admin*` with an
  operator-scoped Allow policy and independent MFA. A TOTP device was enrolled
  and a fresh incognito login verified the identity -> MFA -> application path
  on 2026-08-21; application RBAC remains authoritative. Closure evidence is in
  [`archive/as-02-cloudflare-access-mfa.md`](archive/as-02-cloudflare-access-mfa.md).
- DECISION: keep Bot Fight Mode disabled (`fight_mode: false`). The applied
  operator run recorded an API 400/code 10400 when attempting to enable it;
  the live API/catalog/private-route smoke passed with it disabled. Revisit
  only through a dashboard/plan review and an API/mobile/crawler smoke.

## Origin protection

- DONE: UFW permits 80/443 only from managed Cloudflare IPv4/IPv6 ranges and
  retains SSH/out-of-band console access.
- DONE: the daily `Platform Cloudflare range and UFW parity alert` workflow
  runs a read-only parity proof over pinned SSH and opens/updates a GitHub
  alert issue on failure. Configure repository notifications to deliver those
  issue alerts to the operator mailbox; the Cloudflare Certificate
  Transparency alert uses the separately configured Cloudflare email channel.
- OPTIONAL: Authenticated Origin Pulls is later defense-in-depth and does not
  replace Full(strict).

References: [Cloudflare cipher suites](https://developers.cloudflare.com/ssl/edge-certificates/additional-options/cipher-suites/),
[minimum TLS](https://developers.cloudflare.com/ssl/edge-certificates/additional-options/minimum-tls/),
[R2 cache](https://developers.cloudflare.com/cache/interaction-cloudflare-products/r2/).
