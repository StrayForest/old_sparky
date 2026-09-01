# ADR: Admin Edge Protection

- Status: Accepted, active; operator evidence retained
- Date: 2026-08-01
- Last reviewed: 2026-09-01

## Decision

Application RBAC remains authoritative for every admin API and page. Add
Cloudflare Access with MFA to the real operations UI at
`/platform-ops*`, `/api/v1/admin*` and the audited security surface. Edge
identity narrows exposure but never grants an application role.

Public production is already active without the former whole-site Basic Auth.
The scoped Access policy is active and its break-glass/rollback procedure is an
operator-controlled dashboard change; it must not change the public-site Nginx
vhost.

Use a scoped Cloudflare identity/policy; never request or store a Global API
Key. Access configuration and recovery ownership stay in the manual Cloudflare
checklist.
