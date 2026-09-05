# Pre-release audit worklog — 2026-09-05

Status: Final audit evidence recorded and published
Scope: active OldSparky platform under `platform/`; live origin
`https://old-sparky.com`; legacy bot and `sparkydb` excluded by policy.  
Owner: release audit  

This is the working ledger for the full pre-production audit. It is updated
during the audit so the coverage survives context compression. The final
user-facing report is stored separately as
`pre-release-audit-report-2026-09-05.md`.

## Status vocabulary

- `PASS` — checked and no release-blocking issue found in the covered scope.
- `FAIL` — a concrete defect, gap or unsafe release condition was found.
- `BLOCKED` — the check requires unavailable credentials, infrastructure or
  an operator-only action.
- `NOT RUN` — intentionally deferred or not yet reached.

## Audit coverage ledger

| Area | Status | Evidence / next action |
| --- | --- | --- |
| Repository state and instruction hierarchy | PASS | Root/platform/web guidance, `CURRENT.md`, documentation index and governance read; initial `dev` worktree matched `origin/dev`. |
| Current production baseline and owner docs | PASS WITH OPEN ACTIONS | Active baseline, architecture, security, Cloudflare, backup, deployment and test governance reviewed; Cloudflare/Draft documentation drift recorded. |
| Public live availability and edge contour | PASS WITH OPEN ACTIONS | HTTPS, redirect, TLS certificate, headers, routes, API admission, cache, robots, sitemap, security.txt and error paths probed. |
| Frontend route inventory and rendering | PASS | App Router inventory reviewed; sequential typecheck/build passed and 23 static pages generated. |
| Responsive/mobile and visual UX | PASS WITH LIMITATION | Hermetic desktop/wide/tablet/mobile projects passed; no real-device screenshot or Android Autofill session run. |
| Authentication and account lifecycle | PASS HERMETIC / LIVE BLOCKED | Backend/browser contracts passed; real providers, mailbox, Turnstile and marked live identity require protected QA. |
| Authorization and privacy boundaries | PASS HERMETIC / LIVE CONDITIONAL | DTO/RBAC/negative-path review and tests passed; authenticated live role journey not run. |
| Tournament lifecycle and Deadlock workflows | PASS HERMETIC / LIVE NOT RUN | Full state-machine coverage reviewed and passed locally; no production fixture mutation performed. |
| API contracts and input validation | PASS | Route/schema/error/pagination/idempotency/CORS review plus backend/browser gates passed. |
| Database schema and migrations | PASS | Head `20260903_0052`; migration scenario failed-fast/repaired/upgraded as expected. |
| Background jobs and external integrations | PASS REVIEW / LIVE CONDITIONAL | Celery/Redis/mail/R2/Steam/Google/content paths reviewed; real external calls not triggered. |
| Security and secrets | PASS WITH WARNINGS | Dependency audit, Bandit and 767-file secret scan passed; existing Bandit warnings and backend resource warnings retained. |
| CI/CD and release safety | PASS CODE/CI/LIVE QA | Exact-SHA security/build, auto-deploy, Draft, production deploy and canonical live-public runs passed. |
| Backups, restore and disaster recovery | LOCAL PASS / OFFSITE BLOCKED | Local migration/restore contract passed; documented off-host encrypted recovery drill remains incomplete. |
| Performance, capacity and observability | RETAINED EVIDENCE / LOAD NOT RUN | Existing accepted load evidence and budgets reviewed; no new external load run without operator gate. |
| Documentation and operational readiness | PASS GATE / OPEN ACTIONS | Docs, verification contract and canonical live-public passed; remaining actions are the separately owned perimeter, recovery and product decisions. |
| Full automated verification | LATEST REMOTE PASS / LIVE CONDITIONAL | Local deterministic gates and final exact-SHA run 33931715096 passed; prior run 33931205805 had one `bracket-shell` timeout, recorded as AUD-10 and superseded by the clean pass. |

## Findings register

## Findings recorded

| ID | Priority | Status | Short description |
| --- | --- | --- | --- |
| AUD-01 / AS-12 | P1 release gate | OPEN / operator-owned | Origin listener, Cloudflare CIDR, UFW parity and direct-origin negative proof are still missing. |
| AUD-02 | P1/P2 | OPEN | Cloudflare checklist has stale catalog-cache evidence and multiple unclosed dashboard items. |
| AUD-03 | P2 | VERIFY | Draft mutable responses emit `no-store` but live Cloudflare status is `HIT`; edge semantics need a canary/trace. |
| AUD-04 | P1 continuity gate | INCOMPLETE | Off-host encrypted backup and offline decrypt/checksum recovery are not evidenced. |
| AUD-05 | P2 | OWNER DECISION | Public `/android-autofill-test` diagnostic is `noindex` but not access-controlled. |
| AUD-06 | P2 | CLEANUP | Web-quality passes with four unused-symbol lint warnings. |
| AUD-07 | P2 | CLEANUP | Backend suite passes but emits unclosed Redis/asyncpg resource warnings. |
| AUD-08 | P2 conditional | OWNER DECISION | `www.old-sparky.com` does not resolve; confirm apex-only policy or add canonical redirect. |
| AUD-10 | P1 observation | RESOLVED AFTER RERUN | Exact-SHA run 33931205805 had one Web hermetic timeout at `platform-routes.spec.ts:87`; clean run 33931715096 passed all jobs. |

## Evidence log

### 2026-09-05 — baseline

- Repository: `/root/old_sparky`; branch `dev`; worktree initially clean.
- Current production baseline declares `https://old-sparky.com`, Cloudflare
  Full(strict), Nginx Origin CA, Next.js standalone, FastAPI/Gunicorn, Celery,
  PostgreSQL, Redis and R2/CDN.
- Current documented Alembic head: `20260903_0052`.
- The repository contains a large active regression suite and multiple
  production/operator workflows; detailed execution results will be appended
  after focused review.

### 2026-09-05 — deterministic gates and live evidence

- Backend: `platform_verify.py backend` PASS, 994 tests, `OK`; warnings include
  unclosed Redis/asyncpg resources and slow asyncio callbacks.
- Python quality: PASS. Security: PASS — dependency audit, Bandit and secret
  scan; no secret values copied into the audit.
- Migration: PASS; populated legacy data failed fast, was repaired and
  upgraded.
- Web quality: PASS after sequential rerun; dependency audit/typecheck/build
  passed, lint passed with four unused-symbol warnings, 23 pages generated.
- Web hermetic: PASS — 487 passed, 29 expected skips, plus 8 participant
  progressive tests passed across desktop/wide/tablet/mobile projects.
- Draft: PASS — 35 tests, Node syntax checks and asset build passed.
- Live public curl matrix: HTTPS/HTTP redirect, public pages, protected admin,
  API auth/health boundaries, discovery documents, security headers and
  catalog/private-cache behavior checked.  `/android-autofill-test` is live
  200 and `/draft` mutable assets report `no-store` plus Cloudflare `HIT`.
- GitHub exact-SHA evidence: security/build run `33925699324`, auto-deploy
  `33926101833` and production deploy `33926107320` passed for the reviewed
  source SHA.
- `platform-live-launch.yml` run `33952562061` reached the production browser
  contour for exact source SHA `a4a78deff23c5490838cf3e8a822560230e5f408` and
  passed 44 tests with 10 expected skips across desktop, mobile and WebKit.

### 2026-09-05 — report artifact

- Detailed report: [`pre-release-audit-report-2026-09-05.md`](pre-release-audit-report-2026-09-05.md).
- Generated Playwright report artifact was removed before publication.
- Publication is complete.  Final exact-SHA security/build run
  `33952019381`, auto-deploy `33952324873`, Draft `33952324853` and
  production deploy `33952328897` passed for
  `a4a78deff23c5490838cf3e8a822560230e5f408`; canonical live-public run
  `33952562061` also passed.  The prior 45b timeout in `33931205805` was
  superseded by the clean exact-SHA pass.
  Destructive live-user QA, external load and operator-only perimeter/backup
  actions remain intentionally unavailable to this audit.

## Context-resumption note

When resuming this audit, read this ledger first, then continue from the first
`NOT RUN` row. Do not treat a documented production claim as a fresh live
verification; label repository evidence and operator/live evidence separately.
