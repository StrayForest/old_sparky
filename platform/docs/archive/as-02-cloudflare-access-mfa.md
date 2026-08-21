# AS-02 — Cloudflare Access/MFA privileged-route closure

- Status: Resolved
- Closed: 2026-08-21
- Owner: Production operator

## Finding

Privileged web/API contours needed direct edge evidence that Cloudflare Access and MFA protected them in addition to the application's authoritative RBAC.

## Closure evidence

Operator configuration and live browser verification confirmed:

- the Access application is path-scoped to `/platform-ops*` and `/api/v1/admin*`; the public site remains outside the Access boundary;
- the Allow policy is scoped to the approved operator identity rather than a broad domain/everyone rule;
- Cloudflare independent MFA is required by the privileged-route policy;
- a TOTP MFA device was enrolled through the Access App Launcher and a fresh incognito login completed the Cloudflare identity -> MFA -> application flow;
- `/platform-ops` is intercepted by Cloudflare Access before the Next.js route is reached;
- application `admin`/`superadmin` RBAC remains authoritative after the edge check, so Access never grants an application role;
- the Access application/policy remains separately editable in the authenticated Cloudflare dashboard, providing a bounded rollback path without changing the origin or weakening application RBAC.

No origin bypass, RBAC bypass, broad `Everyone` Allow rule or site-wide Access application was introduced.

## Retained invariant

Cloudflare Access is defense in depth for privileged exposure. The application must continue to reject callers that do not satisfy its own session and role checks even after Cloudflare Access succeeds.
