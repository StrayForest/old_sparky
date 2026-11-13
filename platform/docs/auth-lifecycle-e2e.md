# Automated authentication lifecycle E2E

- Status: Active implementation; exact-source hosted CI validation pending
- Owner: Web and platform maintainers
- Last reviewed: 2026-11-13

The `web-hermetic` gate runs `npm run test:auth-lifecycle-e2e` after the
standalone web build and before separate browser contours. It uses the real
FastAPI routes with a migrated disposable PostgreSQL service and isolated
Redis databases. The browser covers registration, generated-email
verification, login, password reset, password change, session invalidation,
and exact fixture cleanup.

The application generates the email messages; a loopback SMTP sink captures
them in memory for the verification/reset codes. A test-only HTTPX transport
supplies Siteverify responses to the unchanged strict Turnstile verifier. It
exercises provider rejection plus wrong-hostname, wrong-action and missing-
action rejection. Resend and Cloudflare are not contacted. These cases verify
application lifecycle and strict validation logic; they do not prove live
Cloudflare scoring or challenge behavior.

The suite is hosted-CI-only. Preflight requires the exact test mode, loopback
service endpoints and empty disposable database/Redis state before opening
clients. It never flushes shared resources. Separate allowlists construct API
and Next child environments; a source contract proves parent-only secret
sentinels are absent from both. The exact fixture user, sessions, verification
and reset tokens, and audit rows must be absent after cleanup. Per-job services
are disposable if the browser exits early.

Playwright uses a dedicated config and spec selector; generic smoke and live
configs exclude this spec. The contour has one worker, no retries, no traces,
videos, screenshots, HTML report or upload. On failure, the closed reporter
prints only a fixed test/stage ID, source anchor, numeric source line and
bounded timeout/assertion/setup category. It never prints Playwright error
text, form values, email codes, tokens or response bodies.

Production release QA remains source-bound: public QA passes before
authenticated UserQA, which performs exact provisioned-fixture cleanup and
absence proof. Human Chrome and real Turnstile interaction are optional
diagnostics; they are not supplied by this simulated verifier transport.
