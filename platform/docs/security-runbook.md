# Platform security runbook

- Status: Active how-to and policy
- Owner: Security and production operator
- Last reviewed: 2026-09-01

## Security invariants

- Registration cannot grant admin roles; role changes use audited application
  or local operator paths.
- Every login/session path requires an active user.
- Production session cookie is host-only `__Host-`, Secure, HttpOnly,
  SameSite Lax and Path `/`.
- Unsafe cookie requests require same-origin evidence, Fetch Metadata and a
  signed expiring CSRF token.
- Turnstile and Redis limits supplement authentication, invite, support and
  upload controls.
- A successful Turnstile check creates a fixed-term, opaque browser trust grant
  backed by Redis. It is shared by password login, registration and
  password-reset request flows; the one-time Turnstile token is never reused.
- Steam OpenID and optional Google OAuth login/registration use provider-bound,
  single-use callback state and independent rate limits. They do not require a
  second Turnstile check after the ordinary auth form.
- Tokens, passwords, cookies, codes, secrets and personal data never enter
  logs or reports.
- Application RBAC remains authoritative behind Cloudflare Access.

## Public support contact privacy

The configured support-recipient address is server-side operational data. It
must not appear in public HTML, RSC/JSON, metadata, client JavaScript, source
maps or discovery files. Legal/account pages link to `/info#support`.
`/.well-known/security.txt` uses the same HTTPS Contact URI, which is valid
under [RFC 9116](https://www.rfc-editor.org/rfc/rfc9116.html).

Release checks search source and the production `.next` output for the address
and verify public legal/discovery responses contain no `mailto:`. Do not
obfuscate the address with JavaScript/CSS: that still publishes it to bots.

## HTTP and TLS ownership

- The active enforcement release gives document CSP ownership to the Next.js
  document proxy. Nginx owns no CSP header.
- Nginx owns `nosniff`, Referrer-Policy, X-Frame-Options, Permissions-Policy
  and COOP on every response class. Candidate validation rejects either CSP
  header in the Nginx vhost or shared snippet.
- Nginx origin TLS accepts TLS 1.2/1.3 and an explicit ECDHE+AEAD TLS 1.2
  allowlist; CBC and static RSA suites are absent. The installer fails closed
  on policy drift.
- Cloudflare owns visitor-facing TLS suites, certificates, HTTP/3 and HSTS.
  Public SSL scanners measure the edge, not the origin. Do not claim an origin
  cipher edit fixed the edge report.
- HSTS remains single-owner at Cloudflare and is not added to Nginx.

## CSP document contract

The Next.js document proxy owns exactly this policy, where `{nonce}` is the
fresh per-response value:

```text
default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; script-src 'self' 'nonce-{nonce}' https://challenges.cloudflare.com https://static.cloudflareinsights.com https://pagead2.googlesyndication.com; script-src-attr 'none'; style-src 'self' 'nonce-{nonce}'; style-src-attr 'none'; img-src 'self' blob: https://cdn.old-sparky.com https://steamstore-a.akamaihd.net https://clan.fastly.steamstatic.com https://deadlock.io https://assets-bucket.deadlock-api.com https://i2.ytimg.com https://i3.ytimg.com https://pagead2.googlesyndication.com https://googleads.g.doubleclick.net; connect-src 'self' https://pagead2.googlesyndication.com https://googleads.g.doubleclick.net; frame-src https://challenges.cloudflare.com https://googleads.g.doubleclick.net https://tpc.googlesyndication.com; font-src 'self'; manifest-src 'self'; media-src 'none'; worker-src 'self'; report-uri /api/v1/security/csp-report; report-to csp-endpoint
```

No `unsafe-inline`, `unsafe-eval`, `data:` or unreviewed origin is permitted.
The proxy removes client-supplied CSP and `x-nonce` headers, creates 16 random
bytes for every independent document response, gives that nonce to Next.js
rendering and emits exactly one mode-selected response header plus the fixed
`Reporting-Endpoints: csp-endpoint="/api/v1/security/csp-report"` value. The
active release selects `Content-Security-Policy`. Its immediate rollback
release selects `Content-Security-Policy-Report-Only`; that one-line response
header selection is their only reviewed source delta.

The proxy matcher owns HTML documents only. API, RSC, Next static, local asset
and discovery responses have no CSP or Reporting-Endpoints header. Nginx keeps
document proxy caching off and returns `Cache-Control: private, no-store`; API
responses are also uncached/no-store except the explicitly public, short-TTL
tournament catalog. Its cursor pages are safe to cache because they do not
depend on a visitor session; `/tournaments/mine` remains private/no-store.
Fingerprinted Next/static and local asset responses remain separately owned
immutable cache objects and CSP-free.
Cloudflare must not enable Cache Everything on the apex or otherwise cache
nonce-bearing HTML.

The report endpoint is CSRF-exempt but data-minimizing: Nginx caps the body at
32 KiB and applies a per-client `60r/m` zone with `burst=30 nodelay`, returning
429 when limited. The API accepts at most eight report rows, strips query,
fragment, credentials and script samples, and logs only bounded normalized
fields with request correlation. Reports never justify broadening the policy
without reproducing the application need.

Enforcement was activated on 2026-08-13 after the owner explicitly waived the
manual auth/Turnstile contour, repeated enforcement browser contour and
30-minute/24-hour observation windows. These gaps must not be represented as
passed evidence. Keep the live-QA tooling for future targeted validation and
roll back to the Report-Only `previous` release on a confirmed first-party
regression; never weaken the policy merely to silence telemetry.

## Live CSP QA and mailbox boundary

The production gate deliberately has two non-overlapping contours. A root
supervisor creates exactly 14 ephemeral browser sessions with a maximum
one-hour TTL, then hands only their `0600` per-run file to a dedicated non-root,
sandboxed Playwright runner for tournament/roster/bracket behavior. The
database stores only token digests; the plaintext file is reclaimed by root and
destroyed after exact-ID cleanup. This contour never calls auth
lifecycle or Turnstile flows and cannot be cited as auth evidence; it only
reads the session/CSRF endpoints and logs out the short-lived fixtures during
teardown.

Only after that cleanup is proven does an operator use ordinary non-WebDriver
Chrome on a normal end-user network for registration, verification, login,
logout, password reset/change and the real production Turnstile. Cloudflare
documents that Selenium/Playwright are detected as bots and production outcomes
are unpredictable, while dummy keys are restricted to testing environments:
[Turnstile testing](https://developers.cloudflare.com/turnstile/troubleshooting/testing/).
CDP attachment, WebDriver, Xvfb, dummy keys, a protection bypass and Turnstile
token copying/reuse are forbidden.

The root-only manual operator tool obtains email/password values from the
`0600` bundle and reveals them only on an interactive root TTY. The Resend
sent-copy helper contract is `platform_live_qa_mailbox_helper.py code
email-verification|password-reset`; it derives the sole allowed recipient from
that same bundle, so no email, password or code appears in process arguments.
Successful stdout is exactly one six-digit code. It accepts only the exact
expected Russian subject and exactly one sent message at/after the bundle's
no-more-than-four-hour-old `created_at` lower bound. It reads
`PLATFORM_RESEND_API_KEY` only from the fixed root-owned
`/opt/oldsparky/platform/shared/.env.platform` file and makes bounded,
paginated, fixed HTTPS list/retrieve requests to the official sent-email API.
The current key can read sent content and therefore has broader access than a
send-only application key. Keep that capability root-only and time-bounded;
after final QA, rotate the application to `sending_access` and retain no
read-capable QA key unless a separate root-only operational gate still owns it.

The helper does not connect to the database, read OTP rows or invoke a shell;
it ignores a process-environment API key and stores nothing. Failure output is
generic; recipient, message ID/body, API key and OTP must never enter argv,
stderr, journals, QA reports or application logs. The manual attestation must
validate the exact audit sequence, clean only the resolved manual user/session
IDs and prove no marker residue remains. Candidate and enforcement each require
a fresh marker, both contours and both exact cleanups. Because these reviewed
tools run as root to maintain the exact-ID secret boundary, they are not a
filesystem sandbox; the source commit and production origin are part of the
gate.

An interrupted or rejected human run uses only `abort-and-cleanup`. It never
counts as acceptance evidence, but can consume an expired, otherwise unchanged
root-only state to remove zero users or the one exact derived account. It
refuses ambiguous tournament, participant or media scope and requires a fresh
marker for the next attempt.

## Scanner triage

Classify each report before changing production:

- exposed data/header or confirmed unsafe behavior: reproduce and fix;
- framework/library/CDN fingerprint: informational unless paired with an
  affected version/exploit; keep dependencies patched and headers minimal;
- TLS/DNS result: compare public edge, origin/SNI and dashboard separately;
- stale cache/snapshot: retest with date, hostname and cache status;
- unconfirmed heuristic: retain evidence but do not weaken controls to silence
  it.

Next.js/Lucide/React/Cloudflare detection is expected and cannot be reliably
hidden without breaking web contracts. `poweredByHeader: false` and Nginx
`server_tokens off` already suppress optional banners.

## Incident triage

1. Preserve request ID, CF-Ray, UTC interval, route and release; do not dump env.
2. Contain with the smallest reversible control: revoke sessions/credential,
   close an endpoint, pause worker intake or roll back.
3. For auth abuse, adjust Redis/edge controls without permanent account
   lockout. For media abuse, stop new uploads while keeping ready CDN reads.
4. Protect a fresh verified backup and current/previous releases.
5. Follow the incident-response and backup runbooks before destructive action.

## Review cadence

- daily: services, disk, backup age, queue and security/mail/media errors;
- weekly: auth/rate/Turnstile anomalies, CSP reports and pending reconciliation;
- monthly: dependencies, roles/sessions, restore evidence, R2 usage and
  Cloudflare dashboard state;
- every release: secret scan, security gates, preflight, smoke, live role/UI
  checks and rollback compatibility.
