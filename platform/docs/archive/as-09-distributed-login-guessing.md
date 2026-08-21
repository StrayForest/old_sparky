# AS-09 distributed login guessing protection — closed

- Status: Archived / resolved
- Closed: 2026-08-21
- Implementation pull request: `#8`
- Runtime implementation merge: `d07b2f663d64f741a38f4f509231c41bf1742158`
- Pull-request security/build verification run: `32499472769`
- Verified `dev` security/build run: `32500695192`
- Production deployment run: `32501002720`
- Verified production release: `gha-32501002720-1-d07b2f663d64-20260821T160523Z`
- Alembic head: `20260813_0038` (unchanged)

## Original finding

Password-login failure state was scoped to the source IP together with the account identifier. An attacker distributing password guesses across multiple source IPs could therefore avoid building one account-wide failure budget even though a separate per-IP limiter existed. The remediation needed to add shared account throttling without creating a permanent account lockout that could be abused for denial of service, and it needed to preserve identifier privacy in Redis.

## Decision

Password-login protection now uses independent source-IP and account-wide controls.

- The existing per-IP login limiter remains independent and continues to bound request pressure from one source.
- Failed password logins also accumulate in an account-wide window keyed only by an HMAC fingerprint of the normalized account identifier, so changing source IP does not reset the account failure budget.
- Redis keys do not contain plaintext email addresses or source IP addresses.
- Shared account failures participate in adaptive Turnstile requirements.
- Once the account-wide failure threshold is exceeded, a bounded cooldown is created. The default is 60 seconds and is configurable with `PLATFORM_AUTH_LOGIN_ACCOUNT_COOLDOWN_SECONDS`.
- An active cooldown is checked before password verification and blocked requests do not extend the cooldown.
- Starting a cooldown clears the current account failure window so, after cooldown expiry, one additional failed request cannot immediately re-arm another cooldown.
- A successful password login clears both the account failure window and account cooldown state.
- Password-reset recovery retains its separate rate-limit contour; no permanent account lock state was introduced.

This package is intentionally limited to AS-09. Registration enumeration (AS-10), public worker-error sanitization (AS-11), proxy/firewall drift (AS-12) and CI isolation revalidation (AS-13) remain separate findings.

## Verification

Focused AS-09 regression coverage verifies the security invariants:

- failures against one account accumulate across distinct source IPs;
- the independent per-IP limiter remains in place;
- shared failures trigger adaptive Turnstile behavior;
- exceeding the account failure threshold starts a bounded cooldown;
- an active cooldown blocks before a new password guess and does not extend on blocked requests;
- the failure budget is reset when cooldown starts, so cooldown expiry requires a fresh sequence of failures before re-arming;
- successful login clears account failure and cooldown state;
- known and missing account identifiers use the same private HMAC-key shape and do not place plaintext identifiers into Redis state.

Pull request `#8` passed repository security/build run `32499472769` on head `a7ebf107f1fe01257d70f45c1287d3576bc744eb`. After merge, exact source commit `d07b2f663d64f741a38f4f509231c41bf1742158` passed the full `dev` security/build run `32500695192`: backend migrations and unit/integration tests, static/dependency security gates, frontend audit/typecheck/lint/build and Playwright smoke all succeeded.

## Production evidence

Production deployment run `32501002720` checked out exact CI-verified source commit `d07b2f663d64f741a38f4f509231c41bf1742158`, built and checksum-verified immutable release `gha-32501002720-1-d07b2f663d64-20260821T160523Z`, installed it and completed release preflight successfully.

The deployed preflight confirmed a fresh restore-verified database backup and Alembic head `20260813_0038`. After the release switch, `deadlock-api`, `deadlock-worker`, `deadlock-web` and Nginx were active. Direct API live/readiness checks, loopback edge checks and the public `https://old-sparky.com` web/security smoke passed. The public login and registration pages returned successfully, the unauthenticated session endpoint preserved its expected `401` security behavior, and the workflow marked `platform-production-deploy` successful for the verified source SHA.

The closeout does not claim a destructive distributed bad-password probe against a real production account. The account-wide counter/cooldown behavior is covered by focused regression tests; production verification confirms that the exact tested implementation is the active immutable release and that the post-switch auth/web/API contour is healthy.

No database schema, application RBAC, Steam OpenID flow, CSP or Nginx policy was changed by AS-09.

## Remaining scope

AS-02 remains operator-owned Cloudflare Access/MFA verification and AS-14 remains operator-owned HSTS verification. AS-10 requires a product/security decision on registration-enumeration behavior. AS-11 public worker-error sanitization is the next bounded code-owned remediation target; AS-12 and AS-13 remain separate P2 hardening packages.