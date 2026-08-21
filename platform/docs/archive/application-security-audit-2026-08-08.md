# Application Security Audit

- Audit date: 2026-08-08
- CSP follow-up status updated: 2026-08-13
- Scope: `platform/apps/platform_api`, `platform/apps/platform_web`,
  `platform/python_packages`, production Nginx/systemd/release tooling, and the
  Cloudflare-facing trust boundary
- Baseline: deployed source commit
  `db191ef6c375682aad80680841ce8af7e1571fe0`
- Method: manual, read-only code and configuration review mapped to OWASP Top
  10:2021 and relevant CWEs; targeted automated gates are listed below
- Out of scope: legacy Telegram bot, changes to tournament rules or roles,
  destructive production testing, and unauthorised Cloudflare dashboard changes

This document is a point-in-time engineering audit, not a guarantee that the
application is vulnerability-free. No confirmed Critical issue or direct
authentication bypass was found. Two High findings require narrow follow-up;
the remaining findings do not block this release under the accepted release
policy, but remain tracked remediation work.

## Executive summary

| ID | Severity / priority | Confidence | Finding | Status |
|---|---|---:|---|---|
| AS-01 | High / P1 | High | Next.js, API and worker share one Unix identity and one full secret environment | Open |
| AS-02 | High / P1 | High | Cloudflare Access/MFA is unverified; the stale admin-path documentation is corrected here | Open, dashboard action required |
| AS-03 | Medium / P1 | High | Invite-use and participant-capacity checks are not serialised | Open |
| AS-04 | Medium / P1 | High | Inactive participants can satisfy private-workspace access checks | Open |
| AS-05 | Medium / P1 | High | Contact and moderation fields cross the documented public-data boundary | Open |
| AS-06 | Medium / P1 | High | Public SSE has no per-IP/global connection cap | Open |
| AS-07 | Medium / P2 | High | Legacy upload/R2 read paths retain originals and buffer entire objects | Open; removal requires backup/approval |
| AS-08 | Medium / P2 | High | Unknown public patch IDs can trigger synchronous external refresh work | Open |
| AS-09 | Medium / P2 | Medium | Login protection has no account-wide counter across source IPs | Open |
| AS-10 | Low / P2 | High | Registration reveals an existing email address | Open; product decision required |
| AS-11 | Low / P2 | High | Public tournament responses can expose worker exception text | Open |
| AS-12 | Medium / P2 | High | Proxy, Cloudflare-range, UFW and startup validation can drift independently | Open |
| AS-13 | Low / P2 | High | CI security workflow no longer uses the fail-closed test contour | Open |
| AS-14 | Operational / P1 | High | Live HSTS is active although the repository calls it deferred | Verify at Cloudflare; unchanged here |
| AS-15 | High -> resolved | High | Nginx `add_header` inheritance dropped browser-hardening headers | Baseline resolved/live; nonce CSP enforcement active with owner-waived evidence gaps |
| AS-16 | Low -> resolved | High | Public legal/account/security.txt surfaces exposed the support recipient address | Deployed and verified live |

## Authorization matrix

Application RBAC remains authoritative. Cloudflare Access is an additional
exposure control and must never grant an application role.

| Surface | Anonymous | User | Active participant | Organizer | Admin | Superadmin |
|---|---|---|---|---|---|---|
| Public home, patches, public profiles and public tournament summary | Read | Read | Read | Read | Read | Read |
| Invite-only tournament summary | No, unless explicitly public metadata | With valid invite/access | Read | Read | Read | Read |
| Roster, bracket, matches and SSE | Public tournaments: read; invite-only: no | No unless joined | Read | Own tournament: read/manage | Read/manage | Read/manage |
| Own account/profile/media/session | No | Own records only | Own records only | Own records only | Own records only | Own records only |
| Join/leave, ready and captain workflow | No | Subject to workflow eligibility | Own participant actions | Participant actions plus own-tournament management | App rules plus admin operations | App rules plus admin operations |
| Tournament configuration, invites, moderation, bracket/results | No | No | No | Own tournament only | Administrative scope | Administrative scope |
| Admin console/API | No | No | No | No by organizer role alone | Yes | Yes |
| Role grants and destructive pre-production cleanup | No | No | No | No | No | Yes |

Evidence for the role boundary includes the central admin checks in
`apps/platform_api/app/api/routes/admin.py:69`, the superadmin-only cleanup at
`apps/platform_api/app/api/routes/admin.py:594`, the superadmin-only role grant
at `apps/platform_api/app/api/routes/admin.py:910`, and organizer ownership
checks throughout `apps/platform_api/app/api/routes/tournaments.py`. The
inactive-participant exception to the intended matrix is AS-04.

## Findings

### AS-01 — Shared service identity and secret environment

- Severity / priority / confidence: **High / P1 / High**
- CWE / OWASP: CWE-250, CWE-522 / A05 Security Misconfiguration,
  A07 Identification and Authentication Failures
- Preconditions: code execution or environment disclosure in the public
  Next.js process, or compromise of any process running as
  `oldsparky-platform`.
- Evidence: `tools/platform_runtime_common.sh:60` sources and exports every
  value from `.env.platform`; web, API and worker all reference that file and
  use the same account in `deploy/systemd/deadlock-web.service:8`,
  `deploy/systemd/deadlock-api.service:8`, and
  `deploy/systemd/deadlock-worker.service:8`. This conflicts with the
  backend-only credential boundary in `docs/production-architecture.md:64`.
- Impact: a Next.js compromise can expose database, session, R2, Resend and
  Turnstile secrets and can read same-UID process environments or private
  staging data.
- Reproduction: on a staging host, inspect the web service environment and
  compare it with API/worker-only keys; verify same-UID `/proc/<pid>/environ`
  readability without printing values into logs.
- Recommendation: create distinct service users; provide a minimal web env
  containing only API origin/public configuration; use per-service
  `EnvironmentFile=` or systemd credentials; restrict staging paths; add
  `ProtectProc=invisible`/`ProcSubset=pid` where supported. Rotate exposed
  credentials after the boundary changes.
- Status: **Open**. Must be delivered as a narrow runtime-hardening release.

### AS-02 — Admin edge protection is not proven; stale UI targeting is corrected

- Severity / priority / confidence: **High / P1 / High**
- CWE / OWASP: CWE-306, CWE-284 / A01 Broken Access Control,
  A05 Security Misconfiguration
- Preconditions: the public Cloudflare contour remains reachable; the
  application RBAC would still need to be bypassed or credentials compromised
  for privileged actions.
- Evidence: before this audit the ADR called for nonexistent `/admin*`, while
  the actual console is `apps/platform_web/app/platform-ops/page.tsx:16`.
  This release corrects the target in
  `docs/adr/admin-edge-protection.md:8-11`; Access + MFA itself remains TODO in
  `docs/cloudflare-production-checklist.md:62-63`. Application-side admin and
  superadmin checks are present at the locations cited in the authorization
  matrix.
- Impact: the intended second factor/exposure boundary does not protect the
  real admin UI, increasing the value of stolen admin credentials and the
  reachable attack surface.
- Reproduction: unauthenticated requests to `/platform-ops` and
  `/api/v1/admin/overview` should receive a Cloudflare Access challenge before
  an application response; current evidence does not establish that behavior.
- Recommendation: configure Access for `/platform-ops*` and
  `/api/v1/admin*`, require MFA, keep app RBAC, define a tested break-glass path
  and rollback, and correct the ADR/checklist after dashboard proof.
- Status: **Open; external dashboard action required**. It cannot be marked
  complete from repository configuration alone.

### AS-03 — Invite and capacity decisions race

- Severity / priority / confidence: **Medium / P1 / High**
- CWE / OWASP: CWE-362, CWE-367 / A04 Insecure Design
- Preconditions: concurrent claims of a nearly exhausted invite or concurrent
  joins/additions near tournament capacity.
- Evidence: invite state is read and `use_count` incremented without a row lock
  in `apps/platform_api/app/api/routes/tournaments.py:4365-4421`. Participant
  limits are checked before insertion without serialising on the tournament in
  organizer add (`:4611-4670`) and self-join (`:6920-6978`).
- Impact: `max_uses` or `max_participants` can be exceeded; downstream roster
  and workflow invariants can receive an impossible state.
- Reproduction: issue two transactions concurrently against the last invite
  use or last participant slot and hold each before commit; both prechecks can
  observe the same count.
- Recommendation: lock a stable tournament/invite row before check-and-write,
  add database-enforced invariants where practical, and add deterministic
  concurrent integration tests.
- Status: **Open**.

### AS-04 — Inactive participant records retain workspace visibility

- Severity / priority / confidence: **Medium / P1 / High**
- CWE / OWASP: CWE-863 / A01 Broken Access Control
- Preconditions: a user once joined an invite-only tournament and was later
  withdrawn, rejected or disqualified while the participant row remained.
- Evidence: `participant_for_user` does not filter status at
  `apps/platform_api/app/api/routes/tournaments.py:1604`, whereas the separate
  `joined_participant_for_user` correctly excludes inactive states at `:1589`.
  Participant existence is treated as authorization in
  `ensure_tournament_workspace_visible` at `:983` and used by participant,
  match, bracket and SSE reads around `:4559`, `:4925`, and `:4976`.
- Impact: a former participant can continue reading private roster, match,
  bracket and event data.
- Reproduction: join an invite-only tournament, change the participant to an
  inactive state, retain the session, then request the workspace endpoints.
- Recommendation: use the active/joined helper for every workspace gate and
  cover all inactive states with role-matrix tests. Organizer/admin access must
  remain explicit and independent.
- Status: **Open**.

### AS-05 — Public data exceeds the privacy contract

- Severity / priority / confidence: **Medium / P1 / High**
- CWE / OWASP: CWE-200, CWE-359 / A01 Broken Access Control,
  A04 Insecure Design
- Preconditions: an account keeps the registration-seeded contact email or an
  organizer records a moderation note in a public tournament.
- Evidence: registration copies account email to `contact_email` at
  `apps/platform_api/app/api/routes/auth.py:175-201`; the anonymous public
  profile response includes it at
  `apps/platform_api/app/api/routes/profiles.py:240-277`. Public participant
  serialization exposes moderation note, time and moderator ID at
  `apps/platform_api/app/api/routes/tournaments.py:736-751`, and the participant
  list permits anonymous reads for public tournaments at `:4559`. The privacy
  page says email and service logs are not public at
  `apps/platform_web/app/(site)/privacy/page.tsx:42`.
- Impact: personal email and internal moderation context can be disclosed or
  indexed contrary to user expectation and the published notice.
- Reproduction: register without editing the generated profile, request its
  public handle anonymously, and inspect participant JSON after moderation.
- Recommendation: separate public contact opt-in from account email; default it
  to null; return a public participant DTO without moderation fields; migrate
  or obtain explicit consent for existing values; align the privacy notice.
- Status: **Open**.

### AS-06 — Unbounded public SSE connections

- Severity / priority / confidence: **Medium / P1 / High**
- CWE / OWASP: CWE-770 / A04 Insecure Design
- Preconditions: an attacker can open many connections to a public tournament.
- Evidence: public tournaments allow unauthenticated SSE at
  `apps/platform_api/app/api/routes/tournaments.py:4976`; every stream creates
  its own Redis client/pubsub in
  `apps/platform_api/app/services/bracket_events.py:36`; Nginx keeps the
  connection for one hour without `limit_conn` in
  `deploy/nginx/deadlock-platform.conf:102`.
- Impact: file descriptors, API workers and Redis connections can be exhausted,
  reducing availability for normal traffic.
- Reproduction: in pre-production only, ramp idle SSE connections from one
  source IP and observe Nginx active connections, process FDs and Redis clients.
- Recommendation: add per-IP and global connection caps at edge/origin, bound
  stream lifetime and reconnect jitter, expose metrics, and consider one
  process-level subscription with application fan-out.
- Status: **Open**.

### AS-07 — Legacy upload and R2 read contour

- Severity / priority / confidence: **Medium / P2 / High**
- CWE / OWASP: CWE-400, CWE-552 / A05 Security Misconfiguration
- Preconditions: a retained legacy key is requested, or the Nginx alias is
  removed without removing the shadowed FastAPI route.
- Evidence: production still registers a path-key R2 GET proxy in
  `apps/platform_api/app/main.py:41`; the legacy client buffers the complete
  object at `python_packages/platform_infra/object_storage.py:72-88`; Nginx
  currently shadows it with retained local originals at
  `deploy/nginx/deadlock-platform.conf:133`.
- Impact: accidental exposure of retained originals, unbounded API memory use,
  and a surprising route becoming reachable during partial cleanup.
- Reproduction: compare a legacy upload URL at Nginx and directly on the
  loopback API; then inspect behavior for a large existing object in a safe
  environment.
- Recommendation: after an approved, restore-verified backup, inventory and
  sanitise/delete originals, then atomically remove the alias, FastAPI route and
  legacy storage reader; verify old URLs are 404. Do not remove only one layer.
- Status: **Open; destructive remediation requires explicit approval**.

### AS-08 — Patch cache misses trigger external refreshes

- Severity / priority / confidence: **Medium / P2 / High**
- CWE / OWASP: CWE-400, CWE-918 / A04 Insecure Design, A10 SSRF
- Preconditions: an unauthenticated client submits many distinct numeric patch
  IDs absent from cache.
- Evidence: the public route accepts any bounded numeric ID in
  `apps/platform_api/app/api/routes/content.py:84`; a detail miss invokes a
  forced home refresh at
  `apps/platform_api/app/services/home_content.py:919-932`. External starts are
  fixed, so no direct user-selected URL SSRF was found, but the client follows
  redirects at `apps/platform_api/app/services/home_content.py:836` and buffers
  responses.
- Impact: an attacker can repeatedly consume outbound bandwidth and parser
  work; a compromised upstream redirect can broaden the outbound trust path.
- Reproduction: request sequential unknown IDs against a pre-production
  instance while observing outbound requests and refresh latency.
- Recommendation: never force refresh for arbitrary misses; add a negative
  cache, background/global refresh coalescing and a limiter; disable redirects
  or validate every hop against scheme/host/public-IP policy; stream with byte
  caps.
- Status: **Open**.

### AS-09 — Distributed login guessing residual

- Severity / priority / confidence: **Medium / P2 / Medium**
- CWE / OWASP: CWE-307 / A07 Identification and Authentication Failures
- Preconditions: an attacker rotates source addresses for guesses against one
  known account and can solve or outsource Turnstile challenges.
- Evidence: login counters use an IP fingerprint and an `ip-account` fingerprint
  in `python_packages/platform_infra/auth_rate_limit.py:109-117`; failures are
  therefore not aggregated account-wide across IPs (`:166-195`). Turnstile,
  progressive delay and per-IP controls reduce likelihood.
- Impact: a distributed attacker receives more password attempts per account
  than the nominal account limit implies.
- Reproduction: in test, submit failures for one email from distinct trusted
  client-address fixtures and observe independent account counters.
- Recommendation: add a privacy-preserving account-wide failure window and
  bounded cooldown, retain IP/account controls, alert on distributed patterns,
  and avoid permanent lockout DoS.
- Status: **Open**.

### AS-10 — Existing-email enumeration at registration

- Severity / priority / confidence: **Low / P2 / High**
- CWE / OWASP: CWE-204 / A07 Identification and Authentication Failures
- Preconditions: public registration is enabled.
- Evidence: duplicate registration returns an explicit conflict at
  `apps/platform_api/app/api/routes/auth.py:175`. Password reset and
  verification resend intentionally return generic outcomes.
- Impact: attackers can confirm which email addresses have accounts, enabling
  targeted phishing or distributed guessing.
- Reproduction: submit the same valid registration payload for an existing and
  a new address and compare status/body.
- Recommendation: decide explicitly whether duplicate-registration UX justifies
  disclosure. If not, return a generic accepted flow and notify the existing
  address; keep timing and Turnstile behavior comparable.
- Status: **Open; product/security decision required**.

### AS-11 — Worker exception text in public tournament responses

- Severity / priority / confidence: **Low / P2 / High**
- CWE / OWASP: CWE-209 / A05 Security Misconfiguration
- Preconditions: automation raises an exception containing internal details and
  the affected tournament is publicly readable.
- Evidence: `apps/platform_api/app/services/deadlock_automation.py:102` stores
  truncated `str(error)`; the public tournament serializer returns
  `automation_last_error` at
  `apps/platform_api/app/api/routes/tournaments.py:671`.
- Impact: library messages, internal identifiers or operational context may be
  disclosed. No client stack-trace response or FastAPI production debug mode
  was found.
- Reproduction: inject a safe synthetic worker exception in test and request
  the public tournament response.
- Recommendation: persist/return a stable public error code and generic text;
  keep redacted diagnostic detail only in restricted logs/admin views.
- Status: **Open**.

### AS-12 — Proxy and firewall configuration drift

- Severity / priority / confidence: **Medium / P2 / High**
- CWE / OWASP: CWE-346 / A05 Security Misconfiguration
- Preconditions: a production env or firewall is edited independently of the
  reviewed baseline, or Cloudflare removes a CIDR retained in UFW.
- Evidence: the good current defaults bind API/web to loopback and forwarded
  headers to loopback in `tools/platform_configure_shared_env.py:17-25` and
  `tools/platform_run_api.sh:13-32`. Production validation at
  `python_packages/platform_infra/config.py:172-233` and preflight at
  `tools/platform_release_preflight.sh:106-117` do not assert those values.
  The daily updater in
  `deploy/systemd/deadlock-cloudflare-ips.service:12` refreshes Nginx trust but
  does not reconcile UFW.
- Impact: forged client-IP semantics or direct-origin reachability can reappear
  through configuration drift, weakening rate limits and audit attribution.
- Reproduction: in an isolated config test, set a wildcard bind/forwarded
  allowlist and observe that current validation/preflight accepts it; compare
  Nginx trusted ranges with UFW rules.
- Recommendation: add fail-closed production invariants and tests for web/API
  binds, web origin and forwarded allowlist; alert on Nginx/UFW range parity;
  update UFW with an operator-reviewed add-before-remove procedure.
- Status: **Open**.

### AS-13 — CI security workflow uses obsolete isolation settings

- Severity / priority / confidence: **Low / P2 / High**
- CWE / OWASP: CWE-1188 / A05 Security Misconfiguration
- Preconditions: maintainers rely on GitHub checks as equivalent to the local
  fail-closed gate.
- Evidence: `.github/workflows/platform-security.yml:29-57` configures
  `platformdb` and Redis databases 0/1/2, while
  `python_packages/platform_infra/config.py:183-195` now requires
  `platformdb_test`, Redis DB 15 and local object storage for tests. CI also
  invokes `unittest` directly instead of the guarded test runner.
- Impact: the workflow can fail for the wrong reason or stop exercising the
  supported isolation guard, reducing confidence in future security changes.
- Reproduction: execute the workflow environment against the current settings
  validator.
- Recommendation: provision `platformdb_test`, use Redis 15/local storage and
  invoke `tools/platform_run_tests.sh` through the quiet runner.
- Status: **Open**.

### AS-14 — HSTS operational state differs from documentation

- Severity / priority / confidence: **Operational / P1 / High**
- CWE / OWASP: CWE-16 / A05 Security Misconfiguration
- Preconditions: rollback from HTTPS or certificate recovery is required.
- Evidence: the public response on 2026-08-08 returned
  `Strict-Transport-Security: max-age=15552000`, although the pre-audit plan
  called HSTS deferred. The discrepancy is now recorded at
  `docs/cloudflare-production-checklist.md:28-31`. Nginx does not own that
  header, so the observed owner is inferred to be Cloudflare.
- Impact: the actual 180-day browser policy does not match the documented
  staged 300-second rollout/rollback assumption.
- Reproduction: inspect the header through the public Cloudflare contour and
  compare it with an origin/SNI response; verify dashboard ownership.
- Recommendation: immediately verify the Cloudflare HSTS setting and account
  owner, document the real rollback implications, and keep exactly one header
  owner. Do not add or alter HSTS in this release.
- Status: **Dashboard verification required; deliberately unchanged**.

### AS-15 — Browser-hardening headers lost through Nginx inheritance

- Severity / priority / confidence: **High before fix / P1 / High**
- CWE / OWASP: CWE-1021, CWE-693 / A05 Security Misconfiguration
- Preconditions: a browser visits any location containing a local
  `add_header`; Nginx 1.24 does not merge parent `add_header` directives.
- Evidence: the previous server-level policy was shadowed by local cache/header
  directives. This release moves the common set into
  `deploy/nginx/snippets/deadlock-platform-security-headers.conf` and includes
  it in every affected location. Installer and smoke tests cover the pair.
- Impact: clickjacking, MIME-sniffing and referrer protections were absent and
  CSP reports were never collected.
- Reproduction: assert headers on HTML, auth, API, SSE, `_next/static`, assets
  and 404 responses through both origin/SNI and public contours.
- Recommendation: the owner has superseded the former seven-day gate with a
  fail-closed, same-window sequence: nonce-capable Report-Only candidate; full
  release and performance QA; automated tournament/bracket/SSE QA with 14
  short-lived root-created sessions handed only to a dedicated sandboxed
  non-root runner and exact cleanup; separate human auth and
  production Turnstile QA in ordinary Chrome with its own exact cleanup; at
  least 30 clean observation minutes; a separate immutable release whose
  reviewed source delta is the one-line enforcement-header switch; and 24
  hours of follow-up. Violations must be classified without automatically
  adding origins or unsafe sources.
- Status: **Baseline finding resolved and verified live; nonce CSP enforcement
  active with explicitly waived acceptance evidence**. HTML, auth, API,
  SSE, new Next static, cache-busted local assets and 404 responses carry the
  shared policy. An older immutable Cloudflare asset `HIT` retains its
  pre-release metadata until dashboard purge/revalidation; this is tracked
  separately as edge-cache operational work. The owner waived the manual
  auth/Turnstile gate, repeated enforcement browser/SSE gate and 30-minute and
  24-hour observation windows; none is recorded as passed.

### AS-16 — Public support recipient exposed to address harvesters

- Severity / priority / confidence: **Low before fix / P2 / High**
- CWE / OWASP: CWE-200 / A01 Broken Access Control, A05 Security
  Misconfiguration
- Preconditions: anonymous retrieval of legal pages, the account client bundle
  or `/.well-known/security.txt`.
- Evidence: the support address was a literal `mailto:` in
  `components/legal/legal-document-page.tsx`, a client constant in
  `components/profile/profile-editor.tsx`, and the public security contact.
- Impact: automated harvesting, spam and phishing targeting the support
  recipient; this is not an authentication or private-user-data breach.
- Reproduction: fetch privacy/terms/security.txt and search the production Next
  output for the configured address or `mailto:` contact.
- Recommendation: publish only `/info#support`; keep the delivery recipient in
  backend configuration. RFC 9116 permits an HTTPS Contact URI.
- Status: **Resolved, deployed and verified live**. Public legal/account
  surfaces and `security.txt` contain no recipient address or support
  `mailto:`; the production Next output and immutable release bundle were also
  scanned for the removed value.

## External scan reconciliation — 2026-08-08

- Technology detection (Next.js, React, Lucide, PWA, Webpack, Cloudflare and
  HTTP/3) is informational. Optional version banners are already disabled; the
  application cannot promise that framework fingerprints are unobservable.
- The public TLS report accepts several TLS 1.2 ECDHE-CBC compatibility suites.
  Those are Cloudflare edge suites. The origin candidate now enforces TLS
  1.2/1.3 with ECDHE+AEAD only; changing the visitor-facing set is a separate
  Cloudflare plan/compatibility decision.
- One supplied scan reports no HSTS while a dated live response reports a
  180-day edge policy. Dashboard ownership and hostname/cache/date must be
  reconciled before any change; Nginx remains intentionally without HSTS.
- DNS CAA is absent in the supplied report. It is a dashboard hardening choice,
  not proof of certificate compromise; every Cloudflare issuance CA must be
  confirmed before restricting it.
- OCSP stapling, HPKP and session-resumption observations do not establish an
  application vulnerability. HPKP is intentionally not introduced.

## Requested vulnerability classes without a confirmed exploit

The following are negative findings, not permanent guarantees:

| Class | Review result and evidence |
|---|---|
| Authentication bypass / session fixation | No client-supplied session identifier is adopted. Tokens are generated with `secrets.token_urlsafe(48)` and stored as SHA-256 digests (`python_packages/platform_infra/security.py:46-51`); login/reset issue new sessions. |
| CSRF | Unsafe authenticated requests require exact Origin/Referer, Fetch Metadata and a session-bound double-submit token (`python_packages/platform_infra/csrf.py:46-108`, `:146-190`). Public auth mutations require origin validation. |
| XSS | No `dangerouslySetInnerHTML`, `innerHTML`, `eval` or `document.write` sink was found in application web source. External patch HTML is converted to bounded plain text and source images are host-validated. A per-document nonce CSP is enforced without `unsafe-inline` or `unsafe-eval`. |
| SQL injection | Database access uses SQLAlchemy expressions/bound parameters. The only reviewed `text()` health query is a constant `SELECT 1`; no user value is interpolated into SQL. |
| Mass assignment | Handlers assign allowlisted fields explicitly; the only profile `model_dump` occurrence is audit-log serialization, not ORM construction. Pydantic applies type/length/literal validation. Extra-field rejection should be expanded for defence in depth. |
| Direct user-controlled SSRF | No route accepts an arbitrary outbound URL. Turnstile and content providers use configured/fixed origins. AS-08 records redirect and response-size hardening still needed. |
| File upload execution/polyglot | The active media pipeline authenticates owner/purpose, enforces request/staging/byte/rate limits, decodes a single frame with pixel/dimension bounds, and re-encodes immutable WebP variants. AS-07 covers only the retained legacy read contour. |
| CORS | Production does not install `CORSMiddleware`; browsers use same-origin Nginx routing (`apps/platform_api/app/main.py:21-37`). Development uses the exact configured web origin with credentials. |
| Stack traces/debug/admin endpoints | Production disables OpenAPI/Swagger/Redoc (`apps/platform_api/app/main.py:21-29`) and FastAPI debug is not enabled. Structured production logging redacts named secrets (`python_packages/platform_infra/logging.py:9-35`). AS-11 covers stored exception text. |
| Secrets in repository | No committed live credential was identified by review; the secret scan remains a release gate. AS-01 concerns runtime distribution, not a committed value. |

## Confirmed controls

- Password hashing uses the recommended `pwdlib` hasher (Argon2id in current
  runtime), with a dummy hash for unknown-user timing at
  `python_packages/platform_infra/security.py:28-43`.
- Production startup requires a Secure `__Host-` session cookie and enabled
  Turnstile at `python_packages/platform_infra/security.py:54-73`; cookies are
  `HttpOnly`, `Secure`, `SameSite=Lax`, host-only and path `/` at `:84-105`.
- Six-digit codes are HMAC-digested, expire after the configured ten minutes,
  replace older codes, and are consumed under a user/row lock at
  `python_packages/platform_infra/auth_lifecycle.py:74-168` and `:191-242`.
- Password reset returns generic invalid-code errors, changes to Argon2id,
  consumes remaining reset tokens, invalidates sessions and creates a fresh
  session at `apps/platform_api/app/api/routes/auth.py:450-535`.
- Registration, login, password reset, verification, invites, support and media
  have Redis-backed controls; production modes fail closed when their required
  security backends/configuration are unavailable.
- The current Nginx proxy trusts `CF-Connecting-IP` only from the managed
  Cloudflare range include; application proxy headers are accepted only from
  loopback under the approved baseline.
- New media never renders original bytes: it is validated and re-encoded to
  bounded WebP variants with immutable keys and CDN URLs.

## Remediation order and release policy

1. **Next narrow security release:** AS-01, AS-02, AS-03, AS-04, AS-05 and
   AS-06, with concurrency and full role-matrix regression tests.
2. **Subsequent bounded hardening:** AS-07 through AS-13, separated where data
   deletion, network policy or product/privacy decisions require approval.
3. **Operational now:** verify AS-14 in Cloudflare and purge/revalidate stale
   immutable metadata. AS-15 enforcement is active; real-user validation and
   report classification remain the operational fallback after the owner
   waived the manual/repeated-live and observation evidence.
4. Any newly confirmed Critical or direct authentication bypass blocks
   production installation. Other High findings follow the explicitly accepted
   narrow-release policy above rather than prompting an unreviewed architecture
   rewrite in this release.

## Verification evidence for the deployed baseline

- Complete platform tests plus focused auth, public-content, Nginx installer,
  CSP-report and deploy-smoke tests.
- Ruff, Bandit, pip-audit and repository secret scan.
- npm audit, TypeScript, ESLint, Next production build and affected Playwright
  smoke coverage.
- Origin/SNI and public Cloudflare assertions for headers on HTML, auth, API,
  SSE, Next static, local assets and 404 responses.
- The immutable release, restore-verified backup, Alembic head, service
  restart, preflight, expanded deploy smoke and public desktop/mobile/WebKit
  Playwright launch suite passed on 2026-08-08.

## CSP activation evidence and owner-waived gaps

- Fail-closed Report-Only candidate checks for the exact directive allowlist,
  one fresh 128-bit nonce per document, no CSP on non-documents, no nonce HTML
  caching and bounded/sanitized report delivery.
- Complete release and performance QA plus automated tournament/bracket/SSE QA
  through 14 short-lived root-created sessions handed only to the dedicated
  sandboxed non-root runner, followed by automatic exact-ID cleanup.
- Candidate public Chromium/WebKit and automated tournament/bracket/SSE gates
  passed with exact-ID cleanup. The manual registration/verification/login/
  logout/password lifecycle and production Turnstile gate was not run.
- The enforcement artifact used the accepted candidate as its byte-identical
  dependency baseline and passed origin/SNI and public exact-mode smoke. The
  repeated enforcement browser/SSE gate and 30-minute/24-hour observation were
  not run.
- No `unsafe-inline`, `unsafe-eval` or new origin may be added merely to silence
  a report.
