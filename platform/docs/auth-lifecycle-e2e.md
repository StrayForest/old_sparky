# Automated authentication lifecycle E2E

- Status: Required hosted auth and production live QA gates passed
- Owner: Web and platform maintainers
- Last reviewed: 2026-11-14

The `web-hermetic` gate runs `npm run test:auth-lifecycle-e2e` after the
standalone web build and before separate browser contours. It uses the real
FastAPI routes with a migrated disposable PostgreSQL service and isolated
Redis databases. The browser covers registration, generated-email
verification, login, password reset, password change, session invalidation,
and exact fixture cleanup.

Exact-SHA Security run `43569313507/1` passed for source
`d52eb46080fed7a4dbc6c86c4b875cbe11cfa3a5`, including the required hosted
auth lifecycle gate. No separate numeric auth report is available. Production
deploy `43572099804/1` succeeded and automatic sequencer `43572731917/1`,
public child `43572752214/1`, and UserQA child `43573228506/1` all completed
successfully against the same source. Public artifact `14105615416` records
57 attempts: 47 passed and 10 allowlisted expected skips, with zero failed,
flaky, interrupted or timed-out cases; coverage requires all 57 expected
cases and validates each skip annotation. UserQA artifact `14105311501` records
complete coverage, one journey/attempt passed, zero skipped or failed, and
child exit 0. Exact cleanup path and absence assertions passed. Final strict
preflight and origin/public smokes (33 checks, zero failures each) passed.
Final readiness confirmed d52 current and original cb1a previous, C4 selected/
C3 retained, venv origin d52, Alembic current equals head `20261112_0055`,
API/web 200, all three app services active, locks free, no pending systemd jobs,
no operation marker or diagnostic plans, and consistency passed. No workflow
runs remained active. The auth fixture
does not establish production Cloudflare scoring or challenge behavior.

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
