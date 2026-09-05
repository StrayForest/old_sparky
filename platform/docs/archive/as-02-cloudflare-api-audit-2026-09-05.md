# AUD-02 — Cloudflare read-only API audit

- Run: [Platform Cloudflare read-only audit, GitHub Actions run 33963082872](https://github.com/StrayForest/old_sparky/actions/runs/33963082872)
- Source SHA: `4e010d94dafa2c52d407b113bbaa6b1d9e4a509f`
- Date: 2026-09-05
- Mode: GET-only; no Cloudflare setting was changed

## Observed

- The GitHub Actions Cloudflare token is valid and resolves the active
  `old-sparky.com` zone in the configured account.
- R2 bucket `oldsparky` has `r2.dev` public access disabled.
- R2 custom domain `cdn.old-sparky.com` is enabled with active ownership and
  SSL, and minimum TLS 1.2.
- The R2 browser CORS endpoint reported no policy (`404`, Cloudflare error
  `10059`), which is the expected state for browser PUT CORS being absent.
- The only returned Turnstile widget is restricted to `old-sparky.com`.

## Not proven by this token

Cloudflare returned `403` for DNS records, certificate packs and CT alerting,
Cache Rules, WAF/ruleset phases, edge rate limits and Bot Fight Mode. The
current deployment token therefore cannot provide the read-only evidence needed
to close those checklist items. The media-token separation also remains a
dashboard/token-inventory decision and is not exposed by the R2 read endpoints.

AUD-02 remains open. The report is retained as a GitHub artifact for 30 days;
this summary is the durable evidence record.
