# Old Sparky pre-release audit report — 2026-09-05
Status: conditional release decision; this report is an audit record, not a replacement for the protected production workflows.  The report covers the active platform source and the separately deployed Draft application.

## 1. Executive decision
The application has a strong deterministic baseline: the backend suite, migration scenario, web quality gate, hermetic browser suites, Draft tests, dependency audit, repository secret scan and deployment chain all passed for the reviewed source revision.  Anonymous live HTTP smoke checks also found the canonical site available over HTTPS with the expected public/private routing boundaries.
I am **not giving an unconditional production sign-off**.  The release remains conditional because several checks are intentionally owned by the production operator and have not been proven from this audit environment:

1. the open AS-12 origin-perimeter proof (listeners, Cloudflare CIDR parity,
   UFW parity and direct-origin negative test);
2. the dedicated production browser contour with the marked QA identity,
   including real email/Turnstile/provider boundaries and cleanup; and
3. the off-host encrypted backup and offline recovery drill.

There is also an edge-policy verification item: the Draft Worker emits three separate `no-store` directives as designed, while the live response carries `CF-Cache-Status: HIT`.  This is not enough evidence to call it a functional cache leak, but it is inconsistent with the documented “mutable assets cannot remain cached” guarantee and should be closed with an edge-canary test or a Cloudflare explanation before a high-confidence release.
No Critical vulnerability, authentication bypass, public admin exposure, secret leak, or deterministic build/test failure was found in the reviewed scope.

### Decision by area

| Area | Result | Release interpretation |
| --- | --- | --- |
| Repository, source boundaries and instruction compliance | PASS | Active platform and Draft boundaries were audited; legacy bot/database were excluded as required. |
| Python quality and repository security | PASS | Quality, dependency audit, Bandit and secret scan passed; Bandit reported only existing `nosec`/mode warnings. |
| Backend/domain/API tests | PASS | 994 tests completed with `OK`; test-process resource warnings remain cleanup debt. |
| Database migration scenario | PASS | Populated legacy data failed fast, was repaired and upgraded to the documented head. |
| Web typecheck/lint/build | PASS WITH WARNINGS | Production build passed; lint has four unused-symbol warnings. |
| Web hermetic browser coverage | PASS | 487 passed, 29 expected project-specific skips, plus 8 participant-progressive tests passed. |
| Draft local tests/build | PASS | 35 tests passed; syntax check and asset build passed. |
| Anonymous live site availability | PASS | Public routes, API admission boundaries, redirects, headers and discovery documents responded as expected. |
| Cloudflare Access/admin exposure | PASS OBSERVATION | Protected routes redirected to Access; application RBAC remains the authoritative control. |
| Live authenticated user journey | BLOCKED / NOT RUN | Requires the dedicated production QA identity, mailbox helper, provider and Turnstile contour. |
| Origin firewall and forwarded-header perimeter | BLOCKED | Requires root/operator access to the production host; tracked AS-12 remains open. |
| Cloudflare dashboard/R2/WAF/rate configuration | BLOCKED / OPEN | Source and public probes cannot close dashboard-owned items. |
| Off-host restore and disaster recovery | BLOCKED / INCOMPLETE | The runbook explicitly says the off-host capability is incomplete until the recovery drill passes. |
| External load/stress test | NOT RUN | Intentionally not run without the explicit operator load gate and cleanup contract. |

## 2. Scope, evidence and limitations

### Scope
Audited source areas:

- `platform/apps/platform_web` — Next.js App Router, browser components and
  Playwright suites;
- `platform/apps/platform_api` and `platform/python_packages` — FastAPI,
  domain services, repositories, schemas and integrations;
- `platform/alembic` — migrations and migration verification;
- `platform/apps/platform_draft` — Cloudflare Worker, Durable Object, static
  UI and its tests;
- `platform/deploy`, `platform/tools`, `.github/workflows` and
  `platform/tests` — deployment, security, verification and operations;
- current operator and architecture documentation under `platform/docs`.

The retired top-level bot and `sparkydb` were deliberately excluded.  Platform data is scoped to `platformdb`, schema `platform`.
Reviewed source revision at the start of execution:
`3d6ae678c05114fb8bbc33877c98566886488147`
The initial `dev` worktree matched `origin/dev`.  The audit journal itself was created during this work and is tracked separately in [`pre-release-audit-worklog-2026-09-05.md`](pre-release-audit-worklog-2026-09-05.md).

### Evidence classes
The report uses the following evidence boundaries:

- **Repository evidence** — source, tests, configuration and documentation
  inspected locally.
- **Deterministic gate evidence** — commands owned by
  `platform/tools/platform_verify.py` and the Draft package checks.
- **Live public evidence** — unauthenticated requests made to
  `https://old-sparky.com` on 2026-09-05.
- **CI/deployment evidence** — GitHub Actions results for the exact reviewed
  SHA.
- **Operator evidence** — host, Cloudflare dashboard, mailbox, production
  identity, destructive QA and external-load checks.  These are not inferred
  from source code and remain explicitly marked when unavailable.
No `.env` file, token, password, cookie, signed Access redirect or private report was opened or copied into this document.

## 3. Findings register
Severity is release-oriented: P1 means a release gate or a meaningful production safety gap; P2 means a medium risk or a required hardening/cleanup item; P3 is informational or conditional.

### AUD-01 — Origin perimeter proof remains open (P1 release gate / existing AS-12)
**Status:** Open, operator-owned.  **Confidence:** High.
The active security tracker already records AS-12 as open.  The source and deployment tooling now reject unsafe non-loopback binds and untrusted forwarded headers, but the live proof is still missing: listener inspection, exact Cloudflare IPv4/IPv6 range comparison against Nginx and UFW, and a direct-origin negative test.  The authoritative record is [`application-security-audit.md`](../application-security-audit.md), lines 33–38 in this revision.
**Impact:** If the origin is reachable outside the intended Cloudflare path, an
attacker may bypass edge controls, expose origin behavior or influence forwarded-header decisions.  Public Cloudflare success does not prove the origin is unreachable.
**Required closure:** Run the read-only parity proof and direct-origin negative
test on the production VPS, attach the evidence to the security tracker, and keep the result tied to the deployed SHA.  Do not close this from source review alone.

### AUD-02 — Cloudflare checklist and current runtime evidence are out of sync (P1/P2)
**Status:** Open documentation/operator reconciliation.  **Confidence:** High.
`CURRENT.md` records a 2026-09-02 catalog probe showing an edge MISS followed by HIT and the private `/mine` path remaining uncached.  The active [`cloudflare-production-checklist.md`](../cloudflare-production-checklist.md) still contains the older 2026-09-01 `DYNAMIC` result and says the exact catalog Cache Rule is still TODO.  The checklist also leaves CAA review, edge certificate alerts, public R2 settings, WAF tuning, per-action edge rates, Turnstile hostname allowlisting, Bot Fight Mode compatibility and range-update parity as VERIFY/TODO items.
**Impact:** The application can be behaving correctly while the release record
still gives operators contradictory instructions.  That makes it impossible to prove which cache and edge policy was actually reviewed and can cause a future operator to reintroduce an unsafe broad rule or assume a control is complete.
**Required closure:** Re-run the exact catalog and `/mine` probes, update one
owner document with the resulting evidence, and close each dashboard-owned item only with dashboard evidence.  Do not enable Cache Everything on the apex.

### AUD-03 — Draft mutable assets report `HIT` alongside `no-store` (P2 verification risk)
**Status:** Needs edge-canary closure; not confirmed as a user-visible stale
asset defect.  **Confidence:** Medium.
The Worker deliberately sets `Cache-Control: no-store, max-age=0`, `CDN-Cache-Control: no-store` and `Cloudflare-CDN-Cache-Control: no-store` for the Draft shell and mutable JavaScript/CSS files (`platform/apps/platform_draft/worker.js:486–489`).  Live requests to `/draft`, `/draft/app.js`, `/draft/styles.css`, `/draft/draft-core.js` and `/draft/heroes.js` returned all three directives but also returned `CF-Cache-Status: HIT`.
**Impact:** If the `HIT` describes the visitor-facing response rather than an
internal Static Assets lookup, a deployment could serve an old Draft interface after a release.  If it only describes the Worker’s backing asset fetch while the final response is correctly non-cacheable, there is no functional issue; the current headers do not distinguish those interpretations.
**Required closure:** Publish a harmless canary change to a mutable asset or
use the Cloudflare cache trace/dashboard to prove that a new shell is fetched after deployment and that no browser/shared cache retains the old response. Retain the three no-store headers.  Do not “fix” this by weakening the cache policy or by making mutable UI assets immutable without an asset-versioning plan.

### AUD-04 — Off-host backup recovery is not complete (P1 continuity gate)
**Status:** Incomplete by the documented acceptance contract.  **Confidence:**
High.
The local restore-verified backup path is documented and the migration/restore scenario passed.  However, [`backup-restore-runbook.md`](../backup-restore-runbook.md) explicitly keeps off-host backup incomplete until a separate private R2 bucket, separate bucket-scoped token, offline recovery key, root-owned environment and a download/decrypt/checksum recovery drill are evidenced.
**Impact:** A local verified dump protects against some database errors but does
not prove recovery after host, account or storage loss.  The off-host timer is correctly not supposed to be enabled before the manual recovery drill.
**Required closure:** Complete the runbook’s five-item off-host checklist and
perform the offline recovery drill.  Keep the recovery key and private backup artifacts out of the repository and out of audit reports.

### AUD-05 — Production diagnostic route is publicly reachable (P2)
**Status:** Owner decision required.  **Confidence:** High.
`/android-autofill-test` is a real public Next route.  The page is marked `noindex,nofollow` in `platform/apps/platform_web/app/android-autofill-test/page.tsx:9–13`, but it has no authentication or Cloudflare Access gate.  The live route returned HTTP
200.  The page is intentionally a local Android Password Manager diagnostic,
uses an invalid example address, and does not submit to the production API; the audit found no credential or personal-data leak.
**Impact:** A diagnostic/test surface increases production attack surface,
confuses users and may provide an unnecessary place for future test code to accidentally acquire production side effects.  `noindex` is not an access control.
**Required closure:** Before broad public launch, either remove the route from
the production build, put it behind the intended operator/QA boundary, or document an explicit decision that it is a public diagnostic with a maintained threat model and no server-side mutation.

### AUD-06 — Four unused-symbol lint warnings remain (P2 cleanup)
**Status:** Non-blocking today, should be cleaned before release if the affected
admin code is changing.  **Confidence:** High.
The successful web-quality gate reported four warnings and zero errors:

- `components/admin/admin-insights.tsx:40` — `enumLabel`;
- `components/admin/admin-tournaments-page.tsx:4` — `useMemo`;
- `components/admin/admin-tournaments-page.tsx:99` — `formatDate`;
- `components/admin/admin-users-page.tsx:6` — `PlatformApiError`.

**Impact:** Unused imports/props make admin maintenance noisier and can hide a
real regression when a warning budget grows.  They did not fail the current gate.
**Required closure:** Remove the unused symbols or wire them to the intended
behavior, then rerun web-quality and the admin browser scenarios.

### AUD-07 — Backend test process emits unclosed Redis/asyncpg resources (P2 test hygiene)
**Status at audit time:** Tests passed, cleanup debt remained.  **Confidence:** Medium.
The canonical backend run completed with `994 tests ... OK`, but emitted `ResourceWarning` messages for unclosed Redis asyncio connections/transports and an asyncpg connection, plus slow asyncio callback diagnostics.  These warnings were not converted into test failures by the current gate.
**Impact:** A fixture or application shutdown path that leaks only in tests may
also leak during worker restart, integration failure or repeated long-lived operations.  The warnings reduce confidence in resource ownership and make future regressions harder to see.
**Required closure:** Identify the owning fixtures/clients, close them in
`asyncTearDown`/fixture finalizers, and run the backend suite with warnings visible.  Do not hide the warnings globally.

**Resolved 2026-09-05:** same-loop resource ownership, regression coverage and
exact-SHA CI evidence are retained in
[`aud-07-async-resource-lifecycle-2026-09-05.md`](aud-07-async-resource-lifecycle-2026-09-05.md).

### AUD-08 — `www` canonical alias is absent; confirm it is intentional (P2 conditional)
**Status:** Conditional owner decision.  **Confidence:** High for the observed
DNS state; impact depends on the intended public URL.
`https://old-sparky.com` is the documented canonical origin and works.  A request to `https://www.old-sparky.com` failed DNS resolution during the live probe.  The application and Nginx configuration intentionally use the apex host, so this is not an application failure if `www` is deliberately unused.
**Required closure:** Either add a proxied `www` alias that redirects to the
canonical apex, or record in the domain/SEO checklist that `www` is not a supported hostname.  Re-check `robots.txt`/sitemap `Host` compatibility with the chosen canonical policy.

### AUD-10 — Historical exact-SHA remote Web hermetic timeout, clean rerun passed (P1 observation)
**Status:** One timeout occurred on [run 33931205805](https://github.com/StrayForest/old_sparky/actions/runs/33931205805): 486 passed, 29 skipped, 1 failed at `platform-routes.spec.ts:87` while waiting 20 seconds for `bracket-shell`; the subsequent clean exact-SHA [run 33931715096](https://github.com/StrayForest/old_sparky/actions/runs/33931715096) passed all jobs, so no current CI block remains.
**Required follow-up:** Keep the readiness test under observation and investigate if it recurs; do not use silent retries as closure.

## 4. Repository and release audit

### 4.1 Source boundaries and working state
The root and path-specific instructions were read first, followed by `platform/docs/CURRENT.md`, `platform/docs/README.md` and the documentation governance.  The application owner layers are coherent: routes/handlers are thin, domain rules live in services, persistence is in models/migrations and release behavior is in tools/workflows.
The active tree includes separate web, API, worker, PostgreSQL, Redis, R2/CDN, deployment and test areas.  The legacy bot and `sparkydb` were not mixed into the platform audit.  The starting `dev` branch was clean and matched `origin/dev`; the only untracked files observed during the audit were generated browser output and the audit journal, both owned by this audit.  Generated Playwright output was removed before publication.
The repository contains no tracked source maps or obvious debug dumps in the active application tree.  A focused scan of public HTML and static assets did not find backend credentials, support-email leakage or test secrets.

### 4.2 Canonical verification ownership
`platform/tools/platform_verify.py` is the registry used for deterministic verification.  The reviewed registry distinguishes deterministic gates from workflow-only production gates:

- deterministic: `python-quality`, `security`, `docs`,
  `verification-contract`, `backend`, `web-quality`, `web-hermetic` and
  `migration`;
- workflow-only: `server-smoke`, `live-public`, `live-user-destructive` and
  `external-load`.
This separation is correct: local tests do not claim to prove live credentials, the production host, destructive fixture cleanup or capacity behavior.

### 4.3 CI and deployment chain
For the reviewed source SHA, GitHub Actions reported:

- [Platform security and build — run 33925699324](https://github.com/StrayForest/old_sparky/actions/runs/33925699324) — success;
- [Platform auto-deploy — run 33926101833](https://github.com/StrayForest/old_sparky/actions/runs/33926101833) — success;
- [Platform production deploy — run 33926107320](https://github.com/StrayForest/old_sparky/actions/runs/33926107320) — success.

The source and workflow contract require exact-SHA status checks, immutable attested artifacts and a VPS that verifies the artifact rather than resolving dependencies or building from source.  The deployment workflow is therefore a meaningful release-control result, but it does not close the operator-owned perimeter, backup or user-journey gates listed above.
The canonical live-public workflow [run 33952562061](https://github.com/StrayForest/old_sparky/actions/runs/33952562061) completed successfully for exact source SHA `a4a78deff23c5490838cf3e8a822560230e5f408`: 44 tests passed and 10 expected tests were skipped across desktop, mobile and WebKit projects.  The clean result is the release-relevant evidence.

## 5. Deterministic verification results

### 5.1 Python quality and security
Commands were run through the canonical registry from `platform/`.

| Gate | Result | Evidence |
| --- | --- | --- |
| `python-quality` | PASS | `All checks passed!` |
| dependency audit | PASS | No known vulnerabilities found. |
| Bandit | PASS WITH WARNINGS | No blocking finding; warnings were existing `nosec`/file-mode cases in release helpers. |
| tracked-file secret scan | PASS | `Secret scan passed for 767 tracked files.` No secret values were included in this report. |
| `verification-contract` | PASS | Registry/workflow/coverage contract passed. |

The security gate did not reveal a committed token, password, private key or cookie.  That result is limited to repository scanning; it is not a rotation or Cloudflare-account audit.

### 5.2 Backend and domain behavior
The full canonical backend run completed:

```text
Ran 994 tests in 710.001s
OK
[GATE PASS] backend
```

The suite exercises authentication, Steam/OpenID, Google state, sessions, profiles, media descriptors, tournament creation and visibility, invite claims, registration, ready checks, captain/assignment workflows, bracket and match progression, admin operations, content/stats, security reports, background behavior and performance contracts.  The inventory is broad, but no unit-test count should be mistaken for a formal branch-coverage percentage; this audit did not invent a coverage claim where the repository does not publish one.
The observed warnings are recorded as AUD-07.  They did not alter the test result, but they are important because the platform is connection-budgeted.

### 5.3 Database and migration safety
The canonical migration gate passed with the message:

```text
Migration scenario passed: populated legacy data failed fast, was repaired, and upgraded.
[GATE PASS] migration
```

The documented production Alembic head is `20260903_0052`; the platform data boundary is `platformdb`, schema `platform`.  The migration scenario verifies that populated legacy state is not silently accepted and that the repair/upgrade path reaches the expected head.  Production downgrade was not attempted; the runbook correctly treats restores and forward migrations as guarded operations.

### 5.4 Web quality and build
The sequential web-quality rerun passed after the earlier concurrent-build collision was removed:

- dependency audit: `found 0 vulnerabilities`;
- TypeScript typecheck: PASS;
- ESLint: PASS with four warnings recorded as AUD-06;
- Next production build: PASS;
- static-page generation: 23 pages generated;
- no second concurrent Next build was running during the successful build.

The build output showed the expected dynamic/static split: auth, account, tournament, profile, stats and operational pages are server-rendered, while manifest/icon/discovery assets are static.  No build-time secret was exposed.

### 5.5 Hermetic browser suites
The local canonical, exact-b953 and final exact-72fd `web-hermetic` gates passed; one earlier exact-45b docs-only run failed AUD-10 once, then run 33931715096 passed all required jobs:

```text
487 passed (20.8m)
29 skipped
8 passed (1.1m)
[GATE PASS] web-hermetic
```

The 487-test smoke run covered desktop, wide, tablet and mobile-layout projects.  The 29 skips are project-specific mobile/viewport cases selected by the existing Playwright configuration, not hidden failures.  The separate participant-progressive configuration passed all eight desktop/mobile tests.
Covered contracts include:

- native password-manager forms, registration, login, password reset and
  password change;
- adaptive Turnstile mounting and safe provider error states;
- server-resolved auth/header behavior and no duplicate session probes;
- public/private/invite-only tournament rendering and invite-code flows;
- registration, formed/unassigned participant states and deadline behavior;
- profile editor atomicity, captain priority and six-slot hero selection;
- ready-check server-relative timer boundaries and no background polling;
- bracket request-driven/manual refresh behavior;
- list concurrency, pagination, filters and no stale response overwrite;
- admin progressive loading, audited actions and cleanup state;
- mobile overflow/spacing/layout contracts across the configured viewports;
- CSP nonce shape and production-origin restrictions in the browser harness.

This is strong hermetic evidence but not proof that external Steam/Google, mailbox delivery, Turnstile scoring, Cloudflare Access policy or a real production database is healthy.  Those require the operator contours below.

## 6. Live public-origin audit
The live target was the documented canonical origin `https://old-sparky.com`.  The following checks were unauthenticated and did not create accounts, tournaments, invites or production data.

### 6.1 Public route matrix

| Request | Observed result | Assessment |
| --- | --- | --- |
| `/` | 200 | PASS |
| `/tournaments`, `/info`, `/privacy`, `/terms`, `/stats` | 200 | PASS |
| `/auth/login`, `/auth/register`, `/reset-password` | 200 | PASS |
| `/auth/steam-complete`, `/auth/google-complete` without provider state | Safe redirect to login error state, final 200 | PASS; provider failure path is safe |
| `/profile/me`, `/dashboard`, `/organizer` | 200 anonymous shell | PASS; auth UI is rendered without exposing data |
| `/platform-ops` | Access login/redirect | PASS; edge protection observed |
| `/_next/static/does-not-exist.js` | 404 | PASS |
| `/does-not-exist` | 404 | PASS |
| `/android-autofill-test` | 200 | AUD-05 |
| `/draft`, `/draft/` | 200 Draft Worker response | PASS with AUD-03 verification item |
| `/api/v1/health/live`, `/api/v1/health/ready` | 403 public | PASS by design; loopback health is not public |
| `/api/v1/auth/bootstrap` | 401 JSON | PASS |
| `/api/v1/auth/security-config` | 200 JSON | PASS; public capability configuration only |
| `/api/v1/content/home`, `/game-assets`, `/support/status` | 200 JSON | PASS |
| `/api/v1/stats/overview` | 200 JSON | PASS |
| `/api/v1/tournaments` | 200 JSON, currently empty catalog | PASS for the reset baseline; no public fixture data was expected |
| `/api/v1/tournaments/mine` | 401 and no-store | PASS |
| `/api/v1/admin/overview`, `/api/v1/admin/users` | Cloudflare Access redirect | PASS observation |
| `/api/v1/audit/me` | 401 JSON and no-store | PASS |
| `/api/v1/docs`, `/docs`, `/openapi.json` | 404/absent | PASS; production API docs are not public |
| `/.well-known/security.txt` | 200 | PASS |

The empty public catalog is consistent with the documented post-reset state: one retained control account and zero tournaments/participants/workflow/audit rows.  It is not a test of populated catalog card correctness.

### 6.2 HTTPS, TLS and security headers
Observed at the apex:

- HTTP redirects to HTTPS with a 301;
- Cloudflare serves HTTP/2 and advertises HTTP/3;
- certificate subject is `old-sparky.com`, issued by Google Trust Services WE1,
  with observed validity 2026-08-01 through 2026-10-30 and SAN coverage for the   apex and wildcard;
- Cloudflare edge response includes HSTS
  `max-age=15552000`; no `includeSubDomains` or `preload` was observed;
- Nginx/source policy keeps HSTS owned by Cloudflare, avoiding competing HSTS
  owners;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: strict-origin-when-cross-origin`;
- `X-Frame-Options: DENY` on the main platform;
- restrictive `Permissions-Policy`;
- `Cross-Origin-Opener-Policy: same-origin`;
- nonce-based enforced CSP on dynamic HTML, including `script-src-attr
  'none'`, strict-dynamic and the CSP report endpoint;
- public HTML had a fresh CSP nonce and did not expose a support email or
  backend secret.
The certificate observation is edge evidence only; expiry alerting remains a Cloudflare dashboard item (AUD-02).  The origin Nginx policy also rejects unexpected hosts with a default 444/host check, but the direct-origin negative test is still part of AUD-01.

### 6.3 API admission, CORS and cache behavior
The production-origin negative checks found no obvious cross-origin or method bypass:

- an `OPTIONS` request with an unrelated `Origin` to the login endpoint did not
  receive a permissive CORS response and returned the route’s method boundary;
- unsafe method probes against read-only endpoints returned method errors;
- `/api/v1/auth/bootstrap` and `/api/v1/audit/me` rejected anonymous access;
- `/api/v1/tournaments/mine` was private and emitted `no-store`;
- repeated public catalog requests produced the intended short-lived cache
  behavior: the first response was `EXPIRED` and the immediate repeat was   `HIT`, with the origin’s public `max-age=5`, `s-maxage=15` and   `stale-while-revalidate=30` contract;
- query-string catalog requests had their own cache behavior; no private
  `/mine` response was observed at the public cache;
- admin API paths redirected to Cloudflare Access and did not fall through to
  anonymous application data.
The exact Cloudflare rule still needs dashboard reconciliation because the active checklist contains older contradictory evidence (AUD-02).  This is an operator documentation/configuration closure task, not a claim that the observed catalog response was private data.

### 6.4 Discovery and error documents
`robots.txt` was served and disallowed API/auth/profile/reset-password paths; the sitemap was served as XML and contained the canonical public pages.  The response includes a `Host` line with the canonical HTTPS origin.  This should be rechecked against the chosen search-engine compatibility policy together with the optional `www` hostname decision (AUD-08).
`security.txt` contained a support URL, preferred languages, canonical URL, policy URL and a future expiry.  No private support mailbox was published in the response.
The Draft app is a separate edge boundary and was not included in the main Next sitemap.  If Draft discovery is intended to be part of the main SEO surface, add an explicit decision and sitemap strategy; the current edge boundary treats `/draft` as a standalone public tool.

## 7. Authentication, authorization and privacy audit

### 7.1 Authentication lifecycle
Repository review and hermetic scenarios cover password registration/login, email confirmation, password reset, password change, session expiry, Steam OpenID start/callback/linking and Google OAuth state/callback paths.  The code and tests enforce safe return destinations, browser-bound OAuth state, native password-manager fields, account/source-IP guessing protection and adaptive Turnstile behavior.
Live anonymous probes confirmed that bootstrap and audit-self endpoints do not return an anonymous account.  Provider completion routes without valid provider state return a localized safe error/login state.
The following were **not** represented as a live authenticated pass in this audit: successful Steam provider verification, successful Google OAuth, mailbox receipt of verification/reset codes, Turnstile production scoring, logout/session rotation through a real browser, and account recovery with a dedicated QA identity.  They belong to the protected live browser and manual QA contours.

### 7.2 Authorization matrix
The reviewed authorization design keeps application RBAC authoritative and uses Cloudflare Access only as an additional exposure control.  The source and tests cover:

- anonymous public profile and public tournament summary reads;
- invite-only summary/roster/workspace admission;
- active participant-only roster, bracket, ready and captain actions;
- organizer-only management for the organizer’s own tournament;
- admin/superadmin console and audited operations;
- own account/session/profile/media boundaries;
- retained disqualified participants and invite/capacity rules;
- public DTOs that omit contact email, Steam authentication identity,
  moderation note, moderator identity and moderation timestamps;
- separate organizer-management DTOs that retain management-only fields.

Public live paths did not expose admin data or anonymous private workspace data. The live operator user journey remains unrun, so the report does not claim that all interactive authorization paths have been exercised against the live deployment.

### 7.3 CSRF, CORS, rate limits and error handling
The main app installs CSRF middleware, uses same-origin production behavior, keeps CORS disabled in production, applies login/registration/reset and account guessing controls, and sanitizes public automation errors before persistence. The source review found no production API documentation exposure and no credential-bearing values in public HTML.
The remaining edge-rate configuration for register, login, reset, invite, support and upload is explicitly a Cloudflare operator item; application limits remain authoritative.  This is part of AUD-02 rather than an assumption that edge rates are configured.

## 8. Tournament and Deadlock workflow audit
The implementation and hermetic suite cover the major state machine:

1. profile Deadlock card, role and dream-slot editing;
2. public/private tournament creation and monthly/private allowance rules;
3. invite creation, code validation, visibility and claims;
4. registration, capacity and retained participant records;
5. organizer roster management and disqualification/restoration;
6. ready-check eligibility, timer boundaries and vote idempotency;
7. withdrawal/reconciliation and stale automation boundaries;
8. captain round and response behavior;
9. deterministic assignment, stale-run protection, publish/lock transitions;
10. opening round, matches, bracket movement and completion;
11. admin moderation, audited overrides and cleanup behavior.

The browser suite also asserts participant progressive loading, request-driven brackets, no fabricated empty teams, bounded retries and mobile layouts.
The full production fixture journey was not run.  In particular, no new production tournament, invite, participant, captain assignment or completion record was created by this audit.  The dedicated `live-user-destructive` workflow must be used for that contour because it carries exact cleanup and requires explicit operator confirmation.

## 9. Draft edge application audit

### 9.1 Boundary and state model
Draft is intentionally separate from the VPS platform:

- `/draft*` is served by a Cloudflare Worker/Static Assets deployment;
- online rooms use one Durable Object and WebSocket Hibernation;
- solo mode is browser-only;
- there is no PostgreSQL, Redis, Celery, FastAPI or durable draft history;
- active rooms are ephemeral, with documented room/completed lifetimes;
- hero media is read from the versioned `cdn.old-sparky.com` namespace.

The Worker validates team size, timer, ban count, sequence shape and per-team quotas server-side.  Captain tokens are high entropy, stored only as hashes in room state, compared in constant time and excluded from public room state. Spectator and message limits are bounded.  WebSocket requests require the upgrade and allowed origin; static non-GET/HEAD methods are rejected.

### 9.2 Draft checks
The package checks completed successfully:

```text
35 tests passed
node --check worker.js ... passed
Prepared 5 Draft assets under dist/draft/
```

The tests cover standard/community/custom sequences, ban/pick quotas, finite timers, duplicate-hero rejection, timeout actions, room creation, origin handling, ready lobby, reconnection, spectators, and stale/result-route behavior.

### 9.3 Draft live probe
The live edge returned:

- `/draft`, `/draft/` — 200 HTML;
- `/draft/app.js`, `/draft/styles.css`, `/draft/draft-core.js`,
  `/draft/heroes.js` — 200 with expected content types;
- `/draft/result` — 404, consistent with the newer “no separate result route”
  decision in the latest code;
- `/draft/does-not-exist` — the Draft shell, marked `X-Robots-Tag:
  noindex,nofollow`, consistent with the room-shell matcher’s current behavior;
- `/draft/api/rooms` without POST creation handling — 404;
- `/draft/ws/nope123` without WebSocket upgrade — 426 `WebSocket required`.

The Draft CSP is intentionally separate from the main nonce CSP.  It includes `unsafe-inline` for its static UI and broad HTTPS image/connect allowances for the edge tool.  That is not a main-site CSP bypass, but it should remain a deliberate, reviewed boundary.  The no-store/HIT ambiguity is AUD-03.
The accepted public Draft ADR still contains historical text about encoding a completed result at `/draft/result` while its header says that decision was superseded in part and the current code intentionally returns 404.  Reconcile that stale paragraph so the owner document describes one result-link contract.

## 10. Frontend, UX, accessibility and responsive behavior
The hermetic matrix exercised desktop, wide, tablet and mobile layout projects. It includes route hierarchy, mobile overflow assertions, navigation state, empty states, form semantics, password manager/autofill behavior, profile editing, tournament cards, bracket zoom/pan and admin panels.  The build produced no type errors and no browser test failures.
Specific positive contracts verified include:

- native credential fields remain usable by password managers;
- Turnstile mounts only when the API asks for it;
- anonymous users do not see an authenticated create form;
- invite-only pages do not silently turn missing access into public data;
- list filters preserve the newest response when requests finish out of order;
- ready-check timers do not generate background request churn;
- tournament and profile actions do not overflow configured mobile viewports;
- the public header/footer and internal route links resolve to valid Next routes;
- the public site keeps CSP and auth state decisions at the server boundary.

No visual regression screenshot baseline or real-device Android Autofill run was available from this audit environment.  The browser tests simulate the contract, while AUD-05 records that the diagnostic page itself is public.

## 11. API, data and external integration audit

### API and data handling
The active API route inventory includes auth, identities, content, health, profiles, profile workspace, stats, tournament catalog/detail/participants, admin, audit, users and security reports.  The source separates public summary DTOs, authenticated workspace data, organizer management data and admin data. Catalog reads use keyset pagination and a rebuildable read model; private `/mine` remains uncached.
Public automation error persistence is sanitized to a stable generic message; restricted diagnostics retain metadata and a one-way fingerprint rather than a raw exception.  Public media is a one-way R2-to-CDN-to-browser flow; no API R2 read fallback or local-disk media serving path was found in the active design.

### External integrations not fully live-proven
The source and tests cover the integration adapters and failure paths for:

- Steam OpenID;
- Google OAuth;
- mail delivery and code confirmation;
- Turnstile;
- R2/CDN media descriptors;
- Redis caches, locks and admission controllers;
- Celery/worker automation;
- patch/content refresh and OpenAI budget boundaries.

No production external call was artificially triggered for credentials, mailbox, OpenAI cache refresh, R2 upload, or provider login during the audit. Those calls belong to the protected live QA or operator runbooks.

## 12. Security audit

### Repository and dependency security
The canonical security gate passed the dependency audit, Bandit and tracked-file secret scan.  Bandit’s output contained warnings for intentional `nosec` cases and file-mode checks in provisioning/release helpers; no gate-blocking issue was reported.  The secret scan covered 767 tracked files.

### Origin and edge security
The source configuration has:

- Nginx default host rejection and explicit canonical host handling;
- loopback-only public health endpoints;
- separated service identities and restricted credentials;
- no HSTS in Nginx while Cloudflare owns visitor HSTS;
- TLS 1.2/1.3 and AEAD-only origin cipher intent;
- Cloudflare Access on platform operations and admin API paths;
- immutable release/artifact verification before activation;
- CSP reporting and rate-limited report intake;
- no production API docs route.

These are good controls, but source intent cannot close the host-level parity proof in AUD-01 or the dashboard items in AUD-02.

### Negative-path probes
The live probe checked anonymous bootstrap/audit paths, public health isolation, admin redirects, unsafe methods, unrelated CORS origins, invalid routes, provider callback errors and the private catalog path.  No anonymous admin or private tournament data was returned.  No active authentication bypass was found.

## 13. Performance, capacity and observability
The repository contains explicit performance profiles, request-performance diagnostics, readiness/admission controls, database connection budgets and exact-cleanup contracts.  `CURRENT.md` retains accepted historical production evidence, including the supported load run, read-mix evidence and Ready Vote capacity model.  The documented SLO contract includes p50/p90/p95/p99 limits, logical failure limits and normal-load shedding expectations.
This audit did not run a new external load/stress/spike/soak test.  That is intentional: the only supported path is the guarded `platform-production-external-load.yml` workflow with an external generator, origin observer, fixture barrier and exact cleanup.  A local HTTP loop or a manually improvised load test would not be equivalent evidence and could risk production capacity.
The backend run’s slow asyncio callback and resource warnings are recorded as AUD-07.  The live public catalog cache behavior was observed, but a fresh capacity number was not inferred from a handful of curl requests.

## 14. Backups, rollback and disaster recovery
The deployment/runbook design has the right safety shape:

- production install is artifact-bound and exact-SHA checked;
- normal deployment is chained from the exact successful security/build run;
- rollback is guarded rather than a source checkout rebuild;
- local backup restore uses a temporary database and checksum/metadata
  validation;
- production restore is explicitly destructive and requires approval;
- the platform scope remains `platformdb`, never `sparkydb`;
- the off-host timer is held behind its restore drill.

The local migration/restore scenario passed, but a local scenario cannot prove an off-host disaster recovery path.  AUD-04 remains a release-readiness gate for a production system expected to survive host/storage loss.

## 15. Documentation and operational readiness
The docs gate and verification-contract gate passed after this report and worklog were edited.  Documentation governance correctly identifies `CURRENT.md` as current-state owner, the documentation index as router and specialized runbooks as owner documents.
The audit found two classes of documentation issue:

1. runtime-vs-checklist drift around the Cloudflare catalog cache rule and
   other dashboard-owned items (AUD-02);
2. stale historical Draft result-route wording that conflicts with the current
   no-result-route behavior.
The source-of-truth documents should be reconciled before claiming a fully closed release checklist.  The long audit journal is intentionally in `docs/archive/`; it is not a replacement for changing the current-state owner documents when an operator action is actually completed.

## 16. Launch checklist mapped to the project QA matrix

| Launch contour | Hermetic/source evidence | Live production evidence | Final status |
| --- | --- | --- | --- |
| Register, login, logout, session persistence | Auth/browser/backend coverage passed | Dedicated real-identity contour not run | CONDITIONAL |
| Edit profile, Deadlock card, roles, dream slots | Browser/backend coverage passed | Not run with live account | CONDITIONAL |
| Create private tournament and auto invite | Browser/backend coverage passed | No production fixture created | CONDITIONAL |
| Claim invite from hub | Browser/backend coverage passed | No destructive live fixture | CONDITIONAL |
| Join by profile/rank/capacity | Browser/backend coverage passed | No destructive live fixture | CONDITIONAL |
| Organizer separation and panel | Browser/admin coverage passed | Access/operator contour not run | CONDITIONAL |
| Ready-check/reconcile/withdraw/disqualify | Backend/browser coverage passed | No production mutation | CONDITIONAL |
| Captain round/responses | Backend/browser coverage passed | No production mutation | CONDITIONAL |
| Assignment/stale run/publish/lock | Backend/browser coverage passed | No production mutation | CONDITIONAL |
| Opening round/matches/bracket/completion | Backend/browser coverage passed | No production mutation | CONDITIONAL |
| Admin grants, moderation, audited overrides | Backend/browser coverage passed; Access redirect observed | Live admin identity not exercised | CONDITIONAL |
| Invite-only anonymous/participant/organizer/admin visibility | Negative tests and DTO review passed | Anonymous boundary sampled; authenticated roles not live-tested | CONDITIONAL |
| Desktop/mobile core screens | Four hermetic viewport projects passed | No real-device screenshot pass | PASS WITH LIMITATION |
| Draft solo and online room | 35 Draft tests and static build passed | Static/route probe passed; live room/WS not joined | CONDITIONAL |
| Backup/restore and rollback | Migration/local restore contracts passed | Off-host recovery and host rollback not run | BLOCKED |
| Edge, WAF, origin and cache policy | Source/header probes passed | Host parity/dashboard proof incomplete | BLOCKED |

## 17. Required actions in release order

### Before declaring the release fully ready

1. Close AUD-01 with the production host perimeter proof.
2. Reconcile AUD-02: catalog Cache Rule, R2 bucket/domain/CORS settings, CAA
   decision, edge certificate alerts, WAF, edge rates, Turnstile hostnames,
   Bot Fight Mode and daily Cloudflare-range/UFW parity.
3. Complete AUD-04’s separate-bucket encrypted upload and offline decrypt/
   checksum recovery drill.
4. If the release requires full user-state confidence, run the explicit
   `live-user-destructive` workflow only with the required confirmation and
   verify its exact cleanup artifact.  Do not replace it with ad hoc production
   requests.
5. Close AUD-03 using an edge-canary or cache trace, and reconcile the Draft
   ADR’s stale result-route paragraph.

### Before or immediately after release, depending on owner policy

7. Remove/protect/document `/android-autofill-test` (AUD-05).
8. Remove the four lint warnings (AUD-06).
9. Fix the async resource ownership warnings (AUD-07).
10. Decide and document the `www` hostname policy and recheck discovery
    documents (AUD-08).

### Explicitly not run by this audit

- destructive live user QA;
- external production load, stress, spike or soak;
- production backup creation/remote upload;
- production restore, migration downgrade or rollback;
- direct-origin access or firewall changes;
- Cloudflare dashboard changes;
- provider login, real mailbox access or Turnstile production interaction.

These omissions are intentional safety boundaries, not silent passes.

## 18. Reproducible command and artifact log
All local gates below were invoked from `platform/` through the canonical registry unless noted otherwise.

| Command/operation | Result |
| --- | --- |
| `./.venv_platform/bin/python tools/platform_verify.py python-quality` | PASS |
| `./.venv_platform/bin/python tools/platform_verify.py security` | PASS; dependency audit, Bandit and secret scan |
| `./.venv_platform/bin/python tools/platform_verify.py migration` | PASS |
| `./.venv_platform/bin/python tools/platform_verify.py backend` | PASS; 994 tests |
| `./.venv_platform/bin/python tools/platform_verify.py web-hermetic` | PASS locally and on final 72fd run 33931715096; prior 45b run 33931205805 had one timeout (AUD-10) |
| `./.venv_platform/bin/python tools/platform_verify.py web-quality` | PASS; build passed, four lint warnings |
| `cd platform/apps/platform_draft && npm test && npm run check && npm run build` | PASS; 35 tests, syntax and asset build |
| `./.venv_platform/bin/python tools/platform_verify.py docs` | PASS after report/worklog edit |
| `./.venv_platform/bin/python tools/platform_verify.py verification-contract` | PASS |
| Anonymous HTTPS/API/edge curl matrix | PASS with AUD-02, AUD-03, AUD-05 and AUD-08 observations |
| `platform-live-launch.yml`, `provision=false` | PASS; exact-SHA run 33952562061, 44 passed and 10 expected skips |
| `platform-live-user-qa.yml` | NOT RUN; destructive and confirmation-gated |
| `platform-production-external-load.yml` | NOT RUN; explicit load/cleanup gate |

## 19. Final audit conclusion
The software baseline is materially stronger than a simple “build passed”: domain/API behavior, auth boundaries, responsive browser contracts, migration repair, release artifact chain, public edge behavior and the separate Draft boundary all have substantial evidence.  The live site is serving the canonical origin and did not expose an obvious anonymous security boundary failure.
The correct release label is therefore:

> **Conditionally ready for controlled release; not fully signed off until the
> operator-owned perimeter, recovery and protected live-user gates are closed.**

The most important operational mistake to avoid is treating the green local suite and successful deployment run as evidence that Cloudflare/UFW parity, off-host recovery, real provider/mailbox flows and destructive cleanup have already been proven.  They have not, and the remaining gates are deliberately visible in this report and the accompanying worklog.
