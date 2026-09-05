# AUD-02 — Cloudflare read-only API audit

- Read-only run: [Platform Cloudflare read-only audit, GitHub Actions run 33964617403](https://github.com/StrayForest/old_sparky/actions/runs/33964617403)
- Evidence artifact: [redacted report](https://github.com/StrayForest/old_sparky/actions/runs/33964617403/artifacts/9969018484)
- Remediation run: [reviewed Cache Rule repair, GitHub Actions run 33964594777](https://github.com/StrayForest/old_sparky/actions/runs/33964594777)
- Source SHA: `233c86f0626ce9edd8664246cb31cf26205ec284`
- Date: 2026-09-05
- Read-only mode: the audit itself made no Cloudflare changes

## Result

The final read-only audit reported `9 PASS` and `8 REVIEW`. The temporary
narrow remediation workflow changed only the reviewed authenticated-catalog
Cache Rule; it replaced the stale `deadlock_platform_session=` cookie match
with the production `__Host-old_sparky_session=` match and verified the
returned rule.

## Confirmed

- The active `old-sparky.com` zone belongs to the configured account.
- Apex DNS and `cdn.old-sparky.com` are proxied; `www.old-sparky.com` has no
  records and no apex AAAA record was returned. This matches the documented
  apex-only policy.
- Production edge certificates include an active Universal certificate and an
  issued Let’s Encrypt backup certificate. Certificate Transparency alerting
  is currently disabled (`enabled: false`).
- R2 bucket `oldsparky` is `Standard`, `r2.dev` public access is disabled,
  browser PUT CORS is absent, and custom domain `cdn.old-sparky.com` has active
  ownership/SSL with minimum TLS 1.2.
- The returned Turnstile widget allowlist contains only `old-sparky.com`.
- The final Cache Rules API response contains the expected session-cookie
  bypass and public catalog rule. Live smoke corroborated the boundary:
  anonymous catalog requests returned `200`/`HIT`, a request with
  `__Host-old_sparky_session` returned `200`/`DYNAMIC`, and
  `/api/v1/tournaments/mine` returned `401`/`no-store`/`DYNAMIC`.
- Bot Fight Mode is disabled (`fight_mode: false`).

## Remaining review items

- CAA policy remains undecided; no CAA records were returned.
- Certificate alerting needs an explicit operator decision and recipient/
  expiry verification.
- Media-token scope and separation from backup credentials are not exposed by
  the Cloudflare read endpoints.
- The custom WAF, managed WAF and edge-rate-limit entrypoints returned no
  configured zone ruleset (`404`, error `10003`); enabling them requires a
  reviewed policy and thresholds.
- Bot Fight Mode runtime compatibility is not applicable while disabled, but
  remains an operator decision before enabling it.
- Daily Cloudflare-range/UFW parity alerting remains an origin/operator check;
  the separate AS-12 perimeter proof is retained in its own archive record.

AUD-02 remains open for these operator-owned controls. No token value or raw
Cloudflare response was stored in the repository; the linked artifact is
redacted and retained for 30 days.
