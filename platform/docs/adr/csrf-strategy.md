# ADR: Cookie CSRF Strategy

- Status: Accepted
- Date: 2026-08-01

## Decision

For unsafe cookie-authenticated requests, require all of:

1. same-origin `Origin`, with a same-origin `Referer` fallback;
2. `Sec-Fetch-Site` not equal to `cross-site`;
3. a signed, expiring double-submit token returned by the CSRF bootstrap route,
   repeated in the CSRF cookie and `X-CSRF-Token` header.

Production cookies use the `__Host-` prefix, Secure, Path `/`, no Domain,
HttpOnly for sessions and SameSite Lax. SameSite and CORS are defense-in-depth,
not substitutes for the token.

Rejected requests return a stable error without reflecting origins/tokens.
Browser code refreshes an expired token once; tests cover missing, mismatched
and cross-site requests.

Reference: [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).
