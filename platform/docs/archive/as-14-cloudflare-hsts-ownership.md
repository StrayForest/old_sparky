# AS-14 — Cloudflare HSTS ownership/state closure

- Status: Resolved
- Closed: 2026-08-21
- Owner: Production operator

## Finding

HSTS ownership and the actual visitor-facing value required direct Cloudflare dashboard verification. The repository policy already required Cloudflare to remain the single HSTS owner and prohibited a second Nginx HSTS header.

## Closure evidence

Direct Cloudflare Edge Certificates dashboard evidence confirmed:

- HTTP Strict Transport Security: **On**;
- Max-Age: **6 months** (`15552000` seconds);
- Include subdomains: **Off**;
- Preload: **Off**;
- Cloudflare remains the visitor-facing HSTS owner.

No HSTS change was required. Nginx must continue to omit `Strict-Transport-Security` so the policy retains one owner.

## Retained invariant

Do not enable `includeSubDomains` or preload without a separate review of every affected hostname and rollback implications. Do not add an origin HSTS header while Cloudflare owns the policy.
