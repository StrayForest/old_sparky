# AUD-02 — Cloudflare operator remediation evidence

- Operator workflow: [run 33966299422](https://github.com/StrayForest/old_sparky/actions/runs/33966299422)
- Source SHA: `9849245204b92e00d3f7d35c5cd89c7e926b5934`
- Date: 2026-09-05
- Mode: applied through a temporary GitHub Actions operator workflow; the
  workflow and its remediation script were removed after the run.

## Applied and verified

- The apex now has an explicit eight-record CAA policy: `issue` and
  `issuewild` for the four Cloudflare partner CAs used by this account
  (`pki.goog`, Let’s Encrypt, SSL.com and Sectigo). The operator run observed
  all eight records present and no missing records.
- Cloudflare Certificate Transparency alerting is enabled. The recipient was
  supplied as a temporary GitHub secret and was not written to the repository
  or this report.
- One zone edge rate-limit rule is active for password-login bursts. The
  current plan exposes one `http_ratelimit` rule slot and a ten-second period;
  the application’s Redis controls remain authoritative for registration and
  password reset.
- The live smoke passed for the homepage, public catalog, private `/mine`
  boundary and Cloudflare edge response after the changes.

## Provider limits recorded

- The zone-level Managed WAF deployment was rejected with Cloudflare’s
  `not entitled to execute this managed ruleset` response. No WAF rule was
  created or claimed.
- Bot Fight Mode remains `fight_mode: false`. The zone update endpoint
  returned HTTP 400/code 10400 even with the current plan-specific writable
  fields preserved. Because Cloudflare documents that Bot Fight Mode can
  challenge API and mobile traffic, the deliberate runtime decision is to
  keep it disabled until the dashboard/plan state is reviewed.
- The account’s available alert types did not include the Universal SSL
  lifecycle alert. Certificate Transparency alerting is enabled; expiry or
  lifecycle alert availability remains a provider-plan review item.

No token value, email address or raw API response was stored in the repository.
