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

Production release QA remains source-bound. The successful production job's
separate secret-free handoff checks the exact deployment receipt and completed
production job, dispatches the sequencer with `workflow_dispatch`, and records
the exact acknowledged sequence run in a parent-owned authorization artifact.
The sequencer waits for the whole parent workflow to succeed and revalidates
that authorization before public QA; authenticated UserQA runs only after
public success and performs exact fixture cleanup and absence proof. A failed
handoff does not fail the app installation and cannot receive QA pass credit;
only the exact parent-issued authorization artifact, not a job summary or job
conclusion, authorizes the sequencer. Sequencer identity is bound to its exact
workflow ID/path, dispatch source/ref/attempt and parent-derived display title;
the run's dynamic API name is not used as workflow identity.
Human Chrome and real Turnstile interaction are optional diagnostics; they
are not supplied by this simulated verifier transport.
