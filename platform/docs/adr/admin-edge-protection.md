# ADR: Admin Edge Protection

- Status: Accepted, activation pending operator setup
- Date: 2026-08-01

## Decision

Application RBAC remains authoritative for every admin API and page. Add
Cloudflare Access with MFA to the real operations UI at
`/platform-ops*`, `/api/v1/admin*` and the audited security surface. Edge
identity narrows exposure but never grants an application role.

Public production is already active without the former whole-site Basic Auth.
Access activation is an operator-controlled dashboard change with an explicit
break-glass and rollback test; it must not change the public-site Nginx vhost.

Use a scoped Cloudflare identity/policy; never request or store a Global API
Key. Access configuration and recovery ownership stay in the manual Cloudflare
checklist.
