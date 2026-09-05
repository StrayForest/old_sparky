# AUD-02 — Cloudflare read-only API audit

- Run: [Platform Cloudflare read-only audit, GitHub Actions run 33963232405](https://github.com/StrayForest/old_sparky/actions/runs/33963232405)
- Source SHA: `9980c6322135a0fc77c5a2e1aa8ce212b00e4a09`
- Date: 2026-09-05
- Mode: GET-only; no Cloudflare setting was changed

## Observed

- The GitHub Actions Cloudflare token is valid and resolves the active
  `old-sparky.com` zone in the configured account.
- R2 bucket `oldsparky` has `r2.dev` public access disabled.
- R2 bucket `oldsparky` uses the `Standard` storage class.
- R2 custom domain `cdn.old-sparky.com` is enabled with active ownership and
  SSL, and minimum TLS 1.2.
- The R2 browser CORS endpoint reported no policy (`404`, Cloudflare error
  `10059`), which is the expected state for browser PUT CORS being absent.
- The only returned Turnstile widget is restricted to `old-sparky.com`.

The run summary was `6 PASS`, `1 REVIEW` and `10 UNAVAILABLE`.

## Not proven by this token

Cloudflare returned `403` for DNS records, certificate packs and CT alerting,
Cache Rules, WAF/ruleset phases, edge rate limits and Bot Fight Mode. The
current deployment token therefore cannot provide the read-only evidence needed
to close those checklist items. The media-token separation also remains a
dashboard/token-inventory decision and is not exposed by the R2 read endpoints.

AUD-02 remains open. The report is retained as a GitHub artifact for 30 days;
this summary is the durable evidence record.
